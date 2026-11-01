use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use mpac_proto::{Backend, BackendType, Model, TestRunRequest, User, UserRateLimits};
use prost::Message;
use sqlx::{PgConnection, PgPool};

const CACHE_TTL: Duration = Duration::from_secs(30);
const CACHE_CAPACITY: usize = 500;

#[derive(Debug, Default)]
pub struct RateLimitCache {
    entries: Mutex<HashMap<String, (UserRateLimits, Instant)>>,
}

impl RateLimitCache {
    pub async fn get(&self, pool: &PgPool, user_id: &str) -> Result<UserRateLimits, sqlx::Error> {
        if let Some(limits) = self.cached(user_id) {
            return Ok(limits);
        }
        let bytes: Option<Vec<u8>> = sqlx::query_scalar(
            "SELECT proto_bytes FROM users WHERE proto_jsonb ->> 'id' = $1 LIMIT 1",
        )
        .bind(user_id)
        .fetch_optional(pool)
        .await?;
        let Some(user) = bytes.and_then(|bytes| User::decode(bytes.as_slice()).ok()) else {
            return Ok(UserRateLimits::default());
        };
        let limits = user.rate_limits.unwrap_or_default();
        self.store(user_id, limits);
        Ok(limits)
    }

    pub fn invalidate(&self, user_id: &str) {
        if let Ok(mut entries) = self.entries.lock() {
            entries.remove(user_id);
        }
    }

    fn cached(&self, user_id: &str) -> Option<UserRateLimits> {
        let entries = self.entries.lock().ok()?;
        entries
            .get(user_id)
            .filter(|(_, inserted)| inserted.elapsed() < CACHE_TTL)
            .map(|(limits, _)| *limits)
    }

    fn store(&self, user_id: &str, limits: UserRateLimits) {
        if let Ok(mut entries) = self.entries.lock() {
            if entries.len() >= CACHE_CAPACITY {
                entries.clear();
            }
            entries.insert(user_id.to_owned(), (limits, Instant::now()));
        }
    }
}

pub async fn current_spend(pool: &PgPool, user_id: &str) -> Result<(f64, f64), sqlx::Error> {
    sqlx::query_as(
        "WITH daily AS (
            SELECT COALESCE(SUM((proto_jsonb->>'total_cost_usd')::float), 0.0) AS total
            FROM test_runs
            WHERE proto_jsonb->>'owner' = $1
              AND created_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
        ),
        monthly AS (
            SELECT COALESCE(SUM((proto_jsonb->>'total_cost_usd')::float), 0.0) AS total
            FROM test_runs
            WHERE proto_jsonb->>'owner' = $1
              AND created_at >= date_trunc('month', NOW() AT TIME ZONE 'UTC')
        )
        SELECT daily.total, monthly.total FROM daily, monthly",
    )
    .bind(user_id)
    .fetch_one(pool)
    .await
}

const DEBUG_RANDOM_FLAT_COST: f64 = 0.001;
const ESTIMATED_INPUT_TOKENS: f64 = 600.0;
const ESTIMATED_OUTPUT_TOKENS: f64 = 100.0;
const ESTIMATED_REASONING_INPUT_TOKENS: f64 = 700.0;
const ESTIMATED_REASONING_OUTPUT_TOKENS: f64 = 300.0;
const DEFAULT_RUN_SAMPLE: i64 = 10;
const DEFAULT_BENCHMARK_SAMPLE: i64 = 25;

#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum LimitError {
    #[error("{0}")]
    Exhausted(String),
    #[error("rate limit lookup failed: {0}")]
    Lookup(String),
}

impl From<sqlx::Error> for LimitError {
    fn from(error: sqlx::Error) -> Self {
        LimitError::Lookup(error.to_string())
    }
}

#[allow(clippy::cast_possible_truncation)]
fn cents(usd: f64) -> i64 {
    (usd * 100.0) as i64
}

#[allow(clippy::cast_precision_loss)]
fn as_f64(value: i64) -> f64 {
    value as f64
}

pub fn estimated_item_cost(model: &Model, include_reasoning: bool) -> f64 {
    let pricing = model.pricing.unwrap_or_default();
    let input = f64::from(pricing.input_token_cost);
    let output = f64::from(pricing.output_token_cost);
    if input <= 0.0 && output <= 0.0 {
        return 0.0;
    }
    let mut cost = ESTIMATED_INPUT_TOKENS * input + ESTIMATED_OUTPUT_TOKENS * output;
    if include_reasoning {
        cost +=
            ESTIMATED_REASONING_INPUT_TOKENS * input + ESTIMATED_REASONING_OUTPUT_TOKENS * output;
    }
    cost
}

pub fn projected_run_cost(request: &TestRunRequest, backend: &Backend) -> Option<f64> {
    if request.models.is_empty() {
        return None;
    }
    if backend.backend_type == i32::from(BackendType::DebugRandom) {
        return Some(DEBUG_RANDOM_FLAT_COST);
    }
    let items = as_f64(if request.sample_size > 0 {
        request.sample_size
    } else {
        DEFAULT_RUN_SAMPLE
    });
    let costs: Vec<f64> = request
        .models
        .iter()
        .map(|model| estimated_item_cost(model, request.include_reasoning))
        .filter(|cost| *cost > 0.0)
        .collect();
    (!costs.is_empty()).then(|| costs.iter().map(|cost| items * cost).sum())
}

pub fn projected_benchmark_cost(
    models: &[Model],
    sample_size: i64,
    include_reasoning: bool,
) -> Option<f64> {
    if models.len() < 2 {
        return None;
    }
    let items = as_f64(if sample_size > 0 {
        sample_size
    } else {
        DEFAULT_BENCHMARK_SAMPLE
    });
    let costs: Vec<f64> = models
        .iter()
        .map(|model| estimated_item_cost(model, include_reasoning))
        .filter(|cost| *cost > 0.0)
        .collect();
    if costs.is_empty() {
        return None;
    }
    let n = as_f64(i64::try_from(models.len()).unwrap_or(i64::MAX));
    Some((2.0 * (n - 1.0) / n) * items * costs.iter().sum::<f64>())
}

impl RateLimitCache {
    pub async fn check_spend_limits(&self, pool: &PgPool, user_id: &str) -> Result<(), LimitError> {
        let limits = self.get(pool, user_id).await?;
        if limits.daily_spend == 0 && limits.monthly_spend == 0 {
            return Ok(());
        }
        let (daily, monthly) = current_spend(pool, user_id).await?;
        if limits.daily_spend > 0 && cents(daily) >= limits.daily_spend {
            return Err(LimitError::Exhausted(format!(
                "Daily spend limit exceeded (${daily:.4} of ${:.2}).",
                as_f64(limits.daily_spend) / 100.0
            )));
        }
        if limits.monthly_spend > 0 && cents(monthly) >= limits.monthly_spend {
            return Err(LimitError::Exhausted(format!(
                "Monthly spend limit exceeded (${monthly:.4} of ${:.2}).",
                as_f64(limits.monthly_spend) / 100.0
            )));
        }
        Ok(())
    }

    pub async fn check_projected_cost(
        &self,
        pool: &PgPool,
        user_id: &str,
        projected: Option<f64>,
        label: &str,
    ) -> Result<(), LimitError> {
        let Some(projected) = projected else {
            return Ok(());
        };
        let limits = self.get(pool, user_id).await?;
        if limits.daily_spend == 0 && limits.monthly_spend == 0 {
            return Ok(());
        }
        let (daily, monthly) = current_spend(pool, user_id).await?;
        for (limit, spent, period) in [
            (limits.daily_spend, daily, "daily"),
            (limits.monthly_spend, monthly, "monthly"),
        ] {
            let remaining = as_f64(limit) / 100.0 - spent;
            if limit > 0 && projected > remaining {
                return Err(LimitError::Exhausted(format!(
                    "Projected {label} cost ${projected:.2} exceeds remaining {period} budget ${remaining:.2}."
                )));
            }
        }
        Ok(())
    }

    pub async fn tpm_exhausted(&self, pool: &PgPool, user_id: &str) -> Result<bool, LimitError> {
        let limits = self.get(pool, user_id).await?;
        if limits.tokens_per_minute == 0 {
            return Ok(false);
        }
        let used: Option<i64> = sqlx::query_scalar(
            "SELECT tokens_used FROM user_tpm_windows
             WHERE user_id = $1 AND window_minute = (EXTRACT(EPOCH FROM NOW())::bigint / 60)",
        )
        .bind(user_id)
        .fetch_optional(pool)
        .await?;
        Ok(used.unwrap_or(0) > limits.tokens_per_minute)
    }

    pub async fn spend_exceeded(
        &self,
        pool: &PgPool,
        user_id: &str,
        in_flight_usd: f64,
    ) -> Result<bool, LimitError> {
        let limits = self.get(pool, user_id).await?;
        if limits.daily_spend == 0 && limits.monthly_spend == 0 {
            return Ok(false);
        }
        let (daily, monthly) = current_spend(pool, user_id).await?;
        Ok(
            (limits.daily_spend > 0 && cents(daily + in_flight_usd) >= limits.daily_spend)
                || (limits.monthly_spend > 0
                    && cents(monthly + in_flight_usd) >= limits.monthly_spend),
        )
    }

    pub async fn record_tpm_in(
        &self,
        pool: &PgPool,
        conn: &mut PgConnection,
        user_id: &str,
        tokens: i64,
    ) -> Result<bool, LimitError> {
        let limits = self.get(pool, user_id).await?;
        if limits.tokens_per_minute == 0 {
            return Ok(true);
        }
        let window = i64::try_from(
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map_or(0, |elapsed| elapsed.as_secs() / 60),
        )
        .unwrap_or(i64::MAX);
        let used: i64 = sqlx::query_scalar(
            "INSERT INTO user_tpm_windows (user_id, window_minute, tokens_used)
             VALUES ($1, $2, $3)
             ON CONFLICT (user_id, window_minute)
             DO UPDATE SET tokens_used = user_tpm_windows.tokens_used + EXCLUDED.tokens_used
             RETURNING tokens_used",
        )
        .bind(user_id)
        .bind(window)
        .bind(tokens)
        .fetch_one(&mut *conn)
        .await?;
        sqlx::query("DELETE FROM user_tpm_windows WHERE user_id = $1 AND window_minute < $2")
            .bind(user_id)
            .bind(window - 2)
            .execute(&mut *conn)
            .await?;
        Ok(used <= limits.tokens_per_minute)
    }
}

#[cfg(test)]
mod tests {
    use mpac_proto::model::ModelPricing;

    use super::*;

    fn priced(input: f32, output: f32) -> Model {
        Model {
            pricing: Some(ModelPricing {
                input_token_cost: input,
                output_token_cost: output,
                ..ModelPricing::default()
            }),
            ..Model::default()
        }
    }

    #[test]
    fn estimated_item_cost_matches_python_formula() {
        let model = priced(0.5, 2.0);

        assert!((estimated_item_cost(&model, false) - (600.0 * 0.5 + 100.0 * 2.0)).abs() < 1e-9);
        assert!(
            (estimated_item_cost(&model, true)
                - (600.0 * 0.5 + 100.0 * 2.0 + 700.0 * 0.5 + 300.0 * 2.0))
                .abs()
                < 1e-9
        );
        assert!(estimated_item_cost(&Model::default(), true).abs() < f64::EPSILON);
    }

    #[test]
    fn projected_run_cost_rules() {
        let request = TestRunRequest {
            models: vec![priced(1.0, 0.0), Model::default()],
            ..TestRunRequest::default()
        };
        let debug = Backend {
            backend_type: BackendType::DebugRandom.into(),
            ..Backend::default()
        };

        assert_eq!(
            projected_run_cost(&request, &debug),
            Some(DEBUG_RANDOM_FLAT_COST)
        );
        assert_eq!(
            projected_run_cost(&request, &Backend::default()),
            Some(10.0 * 600.0)
        );
        assert_eq!(projected_run_cost(&TestRunRequest::default(), &debug), None);
        let unpriced = TestRunRequest {
            models: vec![Model::default()],
            ..TestRunRequest::default()
        };
        assert_eq!(projected_run_cost(&unpriced, &Backend::default()), None);
    }

    #[test]
    fn projected_benchmark_cost_uses_average_bracket_share() {
        let models = vec![
            priced(1.0, 0.0),
            priced(1.0, 0.0),
            priced(1.0, 0.0),
            priced(1.0, 0.0),
        ];

        let cost = projected_benchmark_cost(&models, 0, false).unwrap();

        assert!((cost - (2.0 * 3.0 / 4.0) * 25.0 * 2400.0).abs() < 1e-6);
        assert_eq!(projected_benchmark_cost(&models[..1], 10, false), None);
    }

    #[test]
    fn cents_truncates_like_python_int() {
        assert_eq!(cents(0.019_999), 1);
        assert_eq!(cents(10.0), 1000);
    }
}
