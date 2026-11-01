use std::collections::HashSet;
use std::time::{SystemTime, UNIX_EPOCH};

use jsonwebtoken::{Algorithm, DecodingKey, Validation};
use serde_json::{Map, Value};

pub const AUDIENCES: [&str; 2] = ["mpac-web", "mpac-system"];

#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum JwtError {
    #[error("JWT expired.")]
    Expired,
    #[error("Invalid JWT.")]
    Invalid,
    #[error("JWT missing sub claim.")]
    MissingSubject,
}

fn signature_only() -> Validation {
    let mut validation = Validation::new(Algorithm::HS256);
    validation.validate_exp = false;
    validation.validate_nbf = false;
    validation.validate_aud = false;
    validation.required_spec_claims = HashSet::new();
    validation
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |elapsed| elapsed.as_secs_f64())
}

#[allow(clippy::cast_possible_truncation)]
fn python_int(value: &Value) -> Result<i64, JwtError> {
    match value {
        Value::Bool(flag) => Ok(i64::from(*flag)),
        Value::Number(number) => number
            .as_i64()
            .or_else(|| number.as_f64().map(|f| f.trunc() as i64))
            .ok_or(JwtError::Invalid),
        Value::String(text) => {
            let text = text.trim();
            let digits = text.strip_prefix(['+', '-']).unwrap_or(text);
            let well_formed = !digits.is_empty()
                && !digits.starts_with('_')
                && !digits.ends_with('_')
                && !digits.contains("__")
                && digits.chars().all(|c| c.is_ascii_digit() || c == '_');
            if !well_formed {
                return Err(JwtError::Invalid);
            }
            text.replace('_', "").parse().map_err(|_| JwtError::Invalid)
        }
        _ => Err(JwtError::Invalid),
    }
}

#[allow(clippy::cast_precision_loss)]
fn as_seconds(value: i64) -> f64 {
    value as f64
}

fn validate_claims(claims: &Map<String, Value>, now: f64) -> Result<(), JwtError> {
    if let Some(iat) = claims.get("iat")
        && as_seconds(python_int(iat)?) > now
    {
        return Err(JwtError::Invalid);
    }
    if let Some(nbf) = claims.get("nbf")
        && as_seconds(python_int(nbf)?) > now
    {
        return Err(JwtError::Invalid);
    }
    if let Some(exp) = claims.get("exp")
        && as_seconds(python_int(exp)?) <= now
    {
        return Err(JwtError::Expired);
    }
    let audiences = match claims.get("aud") {
        Some(Value::String(aud)) => vec![aud.as_str()],
        Some(Value::Array(items)) => items
            .iter()
            .map(|item| item.as_str().ok_or(JwtError::Invalid))
            .collect::<Result<Vec<_>, _>>()?,
        _ => return Err(JwtError::Invalid),
    };
    if !audiences.iter().any(|aud| AUDIENCES.contains(aud)) {
        return Err(JwtError::Invalid);
    }
    if claims.get("sub").is_some_and(|sub| !sub.is_string()) {
        return Err(JwtError::Invalid);
    }
    Ok(())
}

pub fn verify(token: &str, secret: &[u8]) -> Result<String, JwtError> {
    let claims = jsonwebtoken::decode::<Map<String, Value>>(
        token,
        &DecodingKey::from_secret(secret),
        &signature_only(),
    )
    .map_err(|_| JwtError::Invalid)?
    .claims;
    validate_claims(&claims, now())?;
    claims
        .get("sub")
        .and_then(Value::as_str)
        .filter(|sub| !sub.is_empty())
        .map(str::to_owned)
        .ok_or(JwtError::MissingSubject)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fixtures::{FIXTURES, cases, field};

    #[test]
    fn matches_pyjwt_verdicts() {
        let secret = FIXTURES["jwt_secret"].as_str().unwrap().as_bytes();
        for case in cases("jwts") {
            let verdict = match verify(field(case, "token"), secret) {
                Ok(_) => "ok",
                Err(JwtError::Expired) => "expired",
                Err(JwtError::Invalid) => "invalid",
                Err(JwtError::MissingSubject) => "missing_sub",
            };
            assert_eq!(verdict, field(case, "expect"), "{}", field(case, "case"));
        }
    }

    fn claims(value: &Value) -> Map<String, Value> {
        value.as_object().unwrap().clone()
    }

    #[test]
    fn exp_equal_to_now_is_expired_like_pyjwt() {
        let now = 1_800_000_000.0;
        assert_eq!(
            validate_claims(
                &claims(&serde_json::json!({"aud": "mpac-web", "exp": 1_800_000_000})),
                now
            ),
            Err(JwtError::Expired)
        );
        assert_eq!(
            validate_claims(
                &claims(&serde_json::json!({"aud": "mpac-web", "exp": 1_800_000_001})),
                now
            ),
            Ok(())
        );
        assert_eq!(
            validate_claims(
                &claims(&serde_json::json!({"aud": "mpac-web", "iat": 1_800_000_000})),
                now
            ),
            Ok(())
        );
    }

    #[test]
    fn python_int_accepts_what_int_accepts() {
        assert_eq!(python_int(&serde_json::json!(" 12 ")), Ok(12));
        assert_eq!(python_int(&serde_json::json!("-1_000")), Ok(-1000));
        assert_eq!(python_int(&serde_json::json!(1.9)), Ok(1));
        assert_eq!(python_int(&serde_json::json!(true)), Ok(1));
        assert_eq!(
            python_int(&serde_json::json!("1.5")),
            Err(JwtError::Invalid)
        );
        assert_eq!(
            python_int(&serde_json::json!("1__0")),
            Err(JwtError::Invalid)
        );
        assert_eq!(python_int(&serde_json::json!(null)), Err(JwtError::Invalid));
    }

    #[test]
    fn returns_subject_for_valid_token() {
        let secret = FIXTURES["jwt_secret"].as_str().unwrap().as_bytes();
        let web = cases("jwts")
            .iter()
            .find(|case| field(case, "case") == "web")
            .unwrap();

        assert_eq!(
            verify(field(web, "token"), secret).unwrap(),
            "user@example.com"
        );
    }
}
