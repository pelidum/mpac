use std::time::SystemTime;

use mpac_proto::json::{JsonError, to_jsonb};
use mpac_proto::{User, UserApiKey, UserRateLimits, UserRole};
use prost::Message;
use sqlx::{PgPool, Postgres, Transaction};

const ONBOARDING_LOCK_KEY: i64 = 0x6d70_6163_6f6e_6264;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct AdminOnboarding {
    pub id: String,
    pub password: Option<String>,
    pub api_key: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OnboardingOutcome {
    AdminAlreadyExists,
    MissingId,
    Created,
    PromotedExistingUser,
}

#[derive(Debug, thiserror::Error)]
pub enum OnboardingError {
    #[error("onboarding query failed: {0}")]
    Query(#[from] sqlx::Error),
    #[error("stored user {0} is not a valid User proto: {1}")]
    Decode(String, #[source] prost::DecodeError),
    #[error(transparent)]
    Json(#[from] JsonError),
}

fn now() -> prost_types::Timestamp {
    prost_types::Timestamp::from(SystemTime::now())
}

fn active_api_key(raw: &str) -> UserApiKey {
    UserApiKey {
        hash: mpac_auth::api_key::hash(raw),
        active: true,
        created_at_utc: Some(now()),
        ..UserApiKey::default()
    }
}

fn new_admin(request: &AdminOnboarding) -> User {
    User {
        id: request.id.clone(),
        role: UserRole::Admin.into(),
        created_at_utc: Some(now()),
        modified_at_utc: Some(now()),
        last_login: Some(now()),
        rate_limits: Some(UserRateLimits::default()),
        api_key: Some(
            request
                .api_key
                .as_deref()
                .map_or_else(UserApiKey::default, active_api_key),
        ),
        password_hash: request
            .password
            .as_deref()
            .map(mpac_auth::password::hash)
            .unwrap_or_default(),
        ..User::default()
    }
}

fn promote(mut user: User, request: &AdminOnboarding) -> User {
    user.role = UserRole::Admin.into();
    if let Some(password) = &request.password {
        user.password_hash = mpac_auth::password::hash(password);
    }
    if let Some(api_key) = &request.api_key {
        let fresh = active_api_key(api_key);
        let key = user.api_key.get_or_insert_with(UserApiKey::default);
        key.hash = fresh.hash;
        key.active = true;
        key.created_at_utc = fresh.created_at_utc;
    }
    user.modified_at_utc = Some(now());
    user
}

async fn admin_exists(tx: &mut Transaction<'_, Postgres>) -> Result<bool, sqlx::Error> {
    sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM users WHERE proto_jsonb->>'role' = 'ADMIN' LIMIT 1)",
    )
    .fetch_one(&mut **tx)
    .await
}

async fn stored_user(
    tx: &mut Transaction<'_, Postgres>,
    id: &str,
) -> Result<Option<User>, OnboardingError> {
    let bytes: Option<Vec<u8>> = sqlx::query_scalar("SELECT proto_bytes FROM users WHERE id = $1")
        .bind(id)
        .fetch_optional(&mut **tx)
        .await?;
    bytes
        .map(|bytes| {
            User::decode(bytes.as_slice())
                .map_err(|error| OnboardingError::Decode(id.into(), error))
        })
        .transpose()
}

pub async fn onboard_admin(
    pool: &PgPool,
    request: &AdminOnboarding,
) -> Result<OnboardingOutcome, OnboardingError> {
    if request.id.is_empty() {
        tracing::warn!("admin onboarding id is empty; skipping onboarding");
        return Ok(OnboardingOutcome::MissingId);
    }
    let mut tx = pool.begin().await?;
    sqlx::query("SELECT pg_advisory_xact_lock($1)")
        .bind(ONBOARDING_LOCK_KEY)
        .execute(&mut *tx)
        .await?;

    if admin_exists(&mut tx).await? {
        tracing::info!("admin user(s) already exist; skipping onboarding");
        return Ok(OnboardingOutcome::AdminAlreadyExists);
    }

    let (user, outcome, sql) = match stored_user(&mut tx, &request.id).await? {
        Some(existing) => (
            promote(existing, request),
            OnboardingOutcome::PromotedExistingUser,
            "UPDATE users SET proto_jsonb = $2::jsonb, proto_bytes = $3 WHERE id = $1",
        ),
        None => (
            new_admin(request),
            OnboardingOutcome::Created,
            "INSERT INTO users (id, proto_jsonb, proto_bytes) VALUES ($1, $2::jsonb, $3)",
        ),
    };
    sqlx::query(sql)
        .bind(&user.id)
        .bind(to_jsonb(&user)?)
        .bind(user.encode_to_vec())
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;

    tracing::info!(admin = %request.id, ?outcome, "onboarded admin");
    Ok(outcome)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request() -> AdminOnboarding {
        AdminOnboarding {
            id: "admin@example.com".into(),
            password: Some("hunter2".into()),
            api_key: Some("sk_test".into()),
        }
    }

    #[tokio::test]
    async fn empty_id_skips_onboarding_without_touching_the_database() {
        let pool = PgPool::connect_lazy("postgres://unreachable.invalid/mpac").unwrap();

        let outcome = onboard_admin(&pool, &AdminOnboarding::default()).await;

        assert_eq!(outcome.unwrap(), OnboardingOutcome::MissingId);
    }

    #[test]
    fn new_admin_matches_python_onboarding_shape() {
        let user = new_admin(&request());

        assert_eq!(user.role, i32::from(UserRole::Admin));
        assert!(mpac_auth::password::verify(&user.password_hash, "hunter2"));
        let key = user.api_key.unwrap();
        assert_eq!(key.hash, mpac_auth::api_key::hash("sk_test"));
        assert!(key.active);
        assert!(user.rate_limits.is_some());
        assert!(user.last_login.is_some());
    }

    #[test]
    fn new_admin_without_credentials_has_empty_key_and_no_password() {
        let user = new_admin(&AdminOnboarding {
            id: "admin@example.com".into(),
            ..AdminOnboarding::default()
        });

        assert_eq!(user.api_key, Some(UserApiKey::default()));
        assert_eq!(user.password_hash, "");
    }

    #[test]
    fn promote_keeps_profile_and_replaces_credentials() {
        let existing = User {
            id: "admin@example.com".into(),
            name: "Existing".into(),
            role: UserRole::Normal.into(),
            api_key: Some(UserApiKey {
                name: "laptop".into(),
                hash: "old".into(),
                ..UserApiKey::default()
            }),
            ..User::default()
        };

        let promoted = promote(existing, &request());

        assert_eq!(promoted.name, "Existing");
        assert_eq!(promoted.role, i32::from(UserRole::Admin));
        let key = promoted.api_key.unwrap();
        assert_eq!(key.name, "laptop");
        assert_eq!(key.hash, mpac_auth::api_key::hash("sk_test"));
        assert!(mpac_auth::password::verify(
            &promoted.password_hash,
            "hunter2"
        ));
    }
}
