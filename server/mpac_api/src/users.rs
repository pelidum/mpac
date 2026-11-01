use std::time::SystemTime;

use mpac_db::crud::{self, Table};
use mpac_proto::{
    GetRequest, ListRequest, LoginRequest, StatusReply, User, UserApiKey, UserRateLimits, UserRole,
    UserSpend,
};
use prost::Message;
use tonic::{Request, Status};

use crate::Api;
use crate::auth::{Caller, caller};
use crate::status::internal;

const DEFAULT_DAILY_SPEND_CENTS: i64 = 1000;
const DEFAULT_MONTHLY_SPEND_CENTS: i64 = 10000;
const MAX_CREDENTIAL_CHARS: usize = 255;
const INVALID_LOGIN: &str = "Invalid username or password";
const DUMMY_PASSWORD_HASH: &str = concat!(
    "scrypt:32768:8:1$bRT3PWGCcnSO0ETo$",
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000",
    "000000000000000000000000000000000000000000000000",
);

fn now() -> prost_types::Timestamp {
    prost_types::Timestamp::from(SystemTime::now())
}

fn strip_secrets(user: &mut User) {
    user.password_hash.clear();
    if let Some(key) = user.api_key.as_mut() {
        key.hash.clear();
    }
}

async fn user_by_id(api: &Api, id: &str) -> Result<Option<User>, Status> {
    let bytes: Option<Vec<u8>> =
        sqlx::query_scalar("SELECT proto_bytes FROM users WHERE proto_jsonb ->> 'id' = $1")
            .bind(id)
            .fetch_optional(&api.pool)
            .await
            .map_err(internal)?;
    Ok(bytes.and_then(|bytes| User::decode(bytes.as_slice()).ok()))
}

async fn verify_password(stored: String, password: String) -> bool {
    tokio::task::spawn_blocking(move || mpac_auth::password::verify(&stored, &password))
        .await
        .unwrap_or(false)
}

async fn visible_user(api: &Api, caller: &Caller, id: &str) -> Result<User, Status> {
    if !caller.admin && id != caller.id {
        return Err(Status::permission_denied("Access denied"));
    }
    let mut user = user_by_id(api, id).await?.unwrap_or_default();
    let (daily_usd, monthly_usd) = mpac_db::rate_limits::current_spend(&api.pool, &user.id)
        .await
        .map_err(internal)?;
    #[allow(clippy::cast_possible_truncation)]
    let spend = UserSpend {
        daily_usd: daily_usd as f32,
        monthly_usd: monthly_usd as f32,
    };
    user.spend = Some(spend);
    strip_secrets(&mut user);
    Ok(user)
}

pub async fn get_user(api: &Api, request: Request<GetRequest>) -> Result<User, Status> {
    let caller = caller(&request)?;
    visible_user(api, &caller, &request.get_ref().id).await
}

pub async fn create_user(api: &Api, request: Request<User>) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    if !caller.admin {
        return Err(Status::permission_denied(
            "Only admins can create new users.",
        ));
    }
    let mut user = request.into_inner();
    user.created_at_utc = Some(now());
    user.modified_at_utc = Some(now());
    user.last_login = Some(prost_types::Timestamp::default());
    user.api_key = Some(UserApiKey::default());
    if user.role != i32::from(UserRole::Admin) {
        let limits = user.rate_limits.get_or_insert_with(UserRateLimits::default);
        if limits.daily_spend == 0 {
            limits.daily_spend = DEFAULT_DAILY_SPEND_CENTS;
        }
        if limits.monthly_spend == 0 {
            limits.monthly_spend = DEFAULT_MONTHLY_SPEND_CENTS;
        }
    }
    crud::create(&api.pool, Table::Users, &user.id.clone(), &user, false)
        .await
        .map_err(internal)
}

pub async fn update_user(api: &Api, request: Request<User>) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    let mut user = request.into_inner();
    let original = visible_user(api, &caller, &user.id).await?;
    if caller.id != original.id && !caller.admin {
        return Err(Status::unauthenticated("No credentials provided."));
    }
    user.modified_at_utc = Some(now());
    user.created_at_utc = Some(original.created_at_utc.unwrap_or_default());
    if let Some(stored) = user_by_id(api, &user.id).await? {
        if user.password_hash.is_empty() && !stored.password_hash.is_empty() {
            user.password_hash = stored.password_hash.clone();
        }
        let stored_key_hash = stored
            .api_key
            .as_ref()
            .map(|key| key.hash.clone())
            .unwrap_or_default();
        if user.api_key.as_ref().is_none_or(|key| key.hash.is_empty())
            && !stored_key_hash.is_empty()
        {
            user.api_key.get_or_insert_with(UserApiKey::default).hash = stored_key_hash;
        }
        if !caller.admin {
            user.role = stored.role;
            user.rate_limits = Some(stored.rate_limits.unwrap_or_default());
            user.org_domain = stored.org_domain;
            user.spend = None;
        }
    }
    let reply = crud::update(&api.pool, Table::Users, &user.id.clone(), &user)
        .await
        .map_err(internal)?;
    api.rate_limits.invalidate(&user.id);
    Ok(reply)
}

pub async fn delete_user(
    api: &Api,
    request: Request<mpac_proto::DeleteRequest>,
) -> Result<StatusReply, Status> {
    let caller = caller(&request)?;
    if !caller.admin {
        return Err(Status::permission_denied("Only admins can delete users."));
    }
    crud::delete(
        &api.pool,
        Table::Users,
        &request.get_ref().id,
        &caller.requester(),
    )
    .await
    .map_err(internal)
}

pub async fn list_users(api: &Api, request: Request<ListRequest>) -> Result<Vec<User>, Status> {
    let caller = caller(&request)?;
    let rows = crud::list_bytes(
        &api.pool,
        Table::Users,
        &caller.requester(),
        request.get_ref().limit,
    )
    .await
    .map_err(internal)?;
    let mut users: Vec<User> = crud::decode_all(rows);
    for user in &mut users {
        user.password_hash.clear();
        if caller.admin {
            if let Some(key) = user.api_key.as_mut() {
                key.hash.clear();
            }
        } else {
            user.api_key = None;
            user.rate_limits = None;
            user.spend = None;
            user.last_login = None;
        }
    }
    Ok(users)
}

pub async fn login_user(api: &Api, request: Request<LoginRequest>) -> Result<User, Status> {
    let LoginRequest { user_id, password } = request.into_inner();
    if user_id.is_empty() || password.is_empty() {
        tracing::warn!("login attempt with missing credentials");
        return Err(Status::invalid_argument(
            "Username and password are required",
        ));
    }
    if user_id.chars().count() > MAX_CREDENTIAL_CHARS
        || password.chars().count() > MAX_CREDENTIAL_CHARS
    {
        return Err(Status::invalid_argument("Invalid credentials"));
    }
    let user = user_by_id(api, &user_id)
        .await
        .map_err(|_| Status::internal("Internal server error"))?;
    let Some(mut user) = user else {
        tracing::warn!(user = %user_id, "login attempt for non-existent user");
        verify_password(DUMMY_PASSWORD_HASH.to_owned(), password).await;
        return Err(Status::unauthenticated(INVALID_LOGIN));
    };
    if user.id.is_empty() {
        return Err(Status::internal("Internal server error"));
    }
    if user.password_hash.is_empty() || !verify_password(user.password_hash.clone(), password).await
    {
        tracing::warn!(user = %user_id, "failed login attempt");
        return Err(Status::unauthenticated(INVALID_LOGIN));
    }
    tracing::info!(user = %user_id, "successful login");
    strip_secrets(&mut user);
    Ok(user)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dummy_hash_matches_python_shape_and_never_verifies() {
        assert_eq!(
            DUMMY_PASSWORD_HASH.len(),
            "scrypt:32768:8:1$bRT3PWGCcnSO0ETo$".len() + 128
        );
        assert!(!mpac_auth::password::verify(DUMMY_PASSWORD_HASH, ""));
    }

    #[test]
    fn strip_secrets_clears_hashes_only() {
        let mut user = User {
            id: "u".into(),
            password_hash: "secret".into(),
            api_key: Some(UserApiKey {
                name: "laptop".into(),
                hash: "h".into(),
                ..UserApiKey::default()
            }),
            ..User::default()
        };

        strip_secrets(&mut user);

        assert_eq!(user.password_hash, "");
        let key = user.api_key.unwrap();
        assert_eq!(key.hash, "");
        assert_eq!(key.name, "laptop");
    }
}
