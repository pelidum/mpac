use std::collections::HashMap;
use std::future::Future;
use std::pin::Pin;
use std::sync::{Arc, Mutex};
use std::task::{Context, Poll};
use std::time::{Duration, Instant};

use http::HeaderMap;
use mpac_auth::jwt;
use mpac_db::crud::{self, Requester};
use sqlx::PgPool;
use tonic::Status;
use tower::{Layer, Service};

pub const PUBLIC_METHODS: [&str; 5] = [
    "/pelidum.services.grpc.mpac.MPAC/LoginUser",
    "/grpc.health.v1.Health/Check",
    "/grpc.health.v1.Health/Watch",
    "/grpc.reflection.v1alpha.ServerReflection/ServerReflectionInfo",
    "/grpc.reflection.v1.ServerReflection/ServerReflectionInfo",
];

const API_KEY_CACHE_TTL: Duration = Duration::from_secs(60);
const API_KEY_CACHE_CAPACITY: usize = 1000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Caller {
    pub id: String,
    pub admin: bool,
    pub web_ui: bool,
}

impl Caller {
    pub fn requester(&self) -> Requester {
        Requester {
            id: self.id.clone(),
            admin: self.admin,
        }
    }
}

#[derive(Debug, Default)]
struct ApiKeyCache {
    entries: Mutex<HashMap<String, (String, Instant)>>,
}

impl ApiKeyCache {
    fn get(&self, key_hash: &str) -> Option<String> {
        let entries = self.entries.lock().ok()?;
        entries
            .get(key_hash)
            .filter(|(_, inserted)| inserted.elapsed() < API_KEY_CACHE_TTL)
            .map(|(user, _)| user.clone())
    }

    fn insert(&self, key_hash: String, user: String) {
        if let Ok(mut entries) = self.entries.lock() {
            if entries.len() >= API_KEY_CACHE_CAPACITY {
                entries.retain(|_, (_, inserted)| inserted.elapsed() < API_KEY_CACHE_TTL);
                if entries.len() >= API_KEY_CACHE_CAPACITY {
                    entries.clear();
                }
            }
            entries.insert(key_hash, (user, Instant::now()));
        }
    }
}

#[derive(Debug)]
pub struct Authenticator {
    pool: PgPool,
    jwt_secret: Option<Vec<u8>>,
    api_keys: ApiKeyCache,
}

impl Authenticator {
    pub fn new(pool: PgPool, jwt_secret: Option<Vec<u8>>) -> Self {
        Self {
            pool,
            jwt_secret: jwt_secret.filter(|secret| !secret.is_empty()),
            api_keys: ApiKeyCache::default(),
        }
    }

    async fn api_key_user(&self, api_key: &str) -> Result<Option<String>, Status> {
        let key_hash = mpac_auth::api_key::hash(api_key);
        if let Some(user) = self.api_keys.get(&key_hash) {
            return Ok(Some(user));
        }
        let user: Option<String> = sqlx::query_scalar(
            "SELECT id FROM users WHERE (proto_jsonb->'api_key'->>'hash') = $1::text LIMIT 1",
        )
        .bind(&key_hash)
        .fetch_optional(&self.pool)
        .await
        .map_err(|error| {
            tracing::error!(%error, "api key lookup failed");
            Status::unauthenticated("Invalid API key.")
        })?;
        if let Some(user) = &user {
            self.api_keys.insert(key_hash, user.clone());
        }
        Ok(user)
    }

    async fn caller(&self, id: String, web_ui: bool) -> Result<Caller, Status> {
        let admin = crud::is_admin(&self.pool, &id).await.map_err(|error| {
            tracing::error!(%error, "admin lookup failed");
            Status::internal("Internal server error")
        })?;
        Ok(Caller { id, admin, web_ui })
    }

    pub async fn authenticate(
        &self,
        method: &str,
        headers: &HeaderMap,
    ) -> Result<Option<Caller>, Status> {
        if PUBLIC_METHODS.contains(&method) {
            return Ok(None);
        }
        let header = |name: &str| {
            headers
                .get_all(name)
                .iter()
                .next_back()
                .and_then(|value| value.to_str().ok())
                .filter(|value| !value.is_empty())
        };

        if let Some(token) = header("x-jwt-token") {
            let Some(secret) = &self.jwt_secret else {
                return Err(Status::unauthenticated("JWT auth not configured."));
            };
            return match jwt::verify(token, secret) {
                Ok(subject) => {
                    tracing::info!(user = %subject, method, "AUTH: ok jwt");
                    self.caller(subject, true).await.map(Some)
                }
                Err(error) => {
                    tracing::warn!(method, %error, "AUTH: fail jwt");
                    Err(Status::unauthenticated(error.to_string()))
                }
            };
        }

        if let Some(api_key) = header("x-api-key") {
            let Some(user) = self.api_key_user(api_key).await? else {
                tracing::warn!(method, "AUTH: fail api_key_invalid");
                return Err(Status::unauthenticated("Invalid API key."));
            };
            tracing::info!(%user, method, "AUTH: ok api_key");
            return self.caller(user, false).await.map(Some);
        }

        tracing::warn!(method, "AUTH: fail no_credentials");
        Err(Status::unauthenticated("Missing credentials."))
    }
}

#[derive(Debug, Clone)]
pub struct AuthLayer {
    authenticator: Arc<Authenticator>,
}

impl AuthLayer {
    pub fn new(authenticator: Arc<Authenticator>) -> Self {
        Self { authenticator }
    }
}

impl<S> Layer<S> for AuthLayer {
    type Service = AuthService<S>;

    fn layer(&self, inner: S) -> Self::Service {
        AuthService {
            inner,
            authenticator: Arc::clone(&self.authenticator),
        }
    }
}

#[derive(Debug, Clone)]
pub struct AuthService<S> {
    inner: S,
    authenticator: Arc<Authenticator>,
}

type BoxFuture<T> = Pin<Box<dyn Future<Output = T> + Send + 'static>>;

impl<S, B> Service<http::Request<B>> for AuthService<S>
where
    S: Service<http::Request<B>, Response = http::Response<tonic::body::Body>>
        + Clone
        + Send
        + 'static,
    S::Future: Send + 'static,
    B: Send + 'static,
{
    type Response = S::Response;
    type Error = S::Error;
    type Future = BoxFuture<Result<Self::Response, Self::Error>>;

    fn poll_ready(&mut self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.inner.poll_ready(cx)
    }

    fn call(&mut self, mut request: http::Request<B>) -> Self::Future {
        let clone = self.inner.clone();
        let mut inner = std::mem::replace(&mut self.inner, clone);
        let authenticator = Arc::clone(&self.authenticator);
        Box::pin(async move {
            let path = request.uri().path().to_owned();
            match authenticator.authenticate(&path, request.headers()).await {
                Ok(Some(caller)) => {
                    request.extensions_mut().insert(caller);
                }
                Ok(None) => {}
                Err(status) => return Ok(status.into_http()),
            }
            inner.call(request).await
        })
    }
}

pub fn caller<T>(request: &tonic::Request<T>) -> Result<Caller, Status> {
    request
        .extensions()
        .get::<Caller>()
        .cloned()
        .ok_or_else(|| Status::unauthenticated("No credentials provided."))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn api_key_cache_expires_and_bounds_entries() {
        let cache = ApiKeyCache::default();
        cache.insert("hash".into(), "user@example.com".into());
        assert_eq!(cache.get("hash").as_deref(), Some("user@example.com"));
        assert_eq!(cache.get("other"), None);

        for index in 0..API_KEY_CACHE_CAPACITY + 5 {
            cache.insert(format!("hash-{index}"), "u".into());
        }
        assert!(cache.entries.lock().unwrap().len() <= API_KEY_CACHE_CAPACITY);
    }

    #[test]
    fn public_methods_match_python_interceptor() {
        assert!(PUBLIC_METHODS.contains(&"/pelidum.services.grpc.mpac.MPAC/LoginUser"));
        assert!(PUBLIC_METHODS.contains(&"/grpc.health.v1.Health/Check"));
        assert!(!PUBLIC_METHODS.contains(&"/pelidum.services.grpc.mpac.MPAC/GetUser"));
    }
}
