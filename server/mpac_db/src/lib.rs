pub mod attachments;
pub mod crud;
pub mod onboarding;
pub mod rate_limits;

use std::time::Duration;

use sqlx::PgPool;
use sqlx::migrate::Migrator;
use sqlx::postgres::{PgConnectOptions, PgPoolOptions};

pub static MIGRATOR: Migrator = sqlx::migrate!("./migrations");

const ACQUIRE_TIMEOUT: Duration = Duration::from_secs(30);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DbConfig {
    pub host: String,
    pub port: u16,
    pub name: String,
    pub user: String,
    pub password: Option<String>,
    pub pool_size: u32,
}

impl DbConfig {
    #[must_use]
    pub fn connect_options(&self) -> PgConnectOptions {
        let options = PgConnectOptions::new();
        let options = if self.host.starts_with('/') {
            options.socket(&self.host)
        } else {
            options.host(&self.host)
        };
        let options = options
            .port(self.port)
            .database(&self.name)
            .username(&self.user);
        match &self.password {
            Some(password) => options.password(password),
            None => options,
        }
    }
}

#[derive(Debug, thiserror::Error)]
pub enum DbError {
    #[error("database connection failed: {0}")]
    Connect(#[source] sqlx::Error),
    #[error("schema migration failed: {0}")]
    Migrate(#[from] sqlx::migrate::MigrateError),
}

pub async fn connect(config: &DbConfig) -> Result<PgPool, DbError> {
    PgPoolOptions::new()
        .max_connections(config.pool_size)
        .acquire_timeout(ACQUIRE_TIMEOUT)
        .connect_with(config.connect_options())
        .await
        .map_err(DbError::Connect)
}

pub async fn migrate(pool: &PgPool) -> Result<(), DbError> {
    MIGRATOR.run(pool).await?;
    tracing::info!(
        applied = MIGRATOR.iter().count(),
        "database schema is up to date"
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn migrations_are_embedded_in_version_order() {
        let versions: Vec<i64> = MIGRATOR.iter().map(|m| m.version).collect();
        let mut sorted = versions.clone();
        sorted.sort_unstable();

        assert_eq!(versions.first(), Some(&1));
        assert_eq!(versions, sorted);
    }

    #[test]
    fn baseline_is_idempotent_ddl() {
        let baseline = MIGRATOR.iter().find(|m| m.version == 1).unwrap();
        for statement in baseline
            .sql
            .as_str()
            .split(';')
            .map(str::trim)
            .filter(|s| !s.is_empty())
        {
            let idempotent = statement.starts_with("UPDATE") || statement.contains("IF NOT EXISTS");
            assert!(idempotent, "non-idempotent baseline statement: {statement}");
        }
    }

    #[test]
    fn absolute_host_is_a_unix_socket_directory() {
        let config = DbConfig {
            host: "/cloudsql/project:region:instance".into(),
            port: 5432,
            name: "mpac".into(),
            user: "mpac".into(),
            password: None,
            pool_size: 2,
        };

        let options = config.connect_options();

        assert_eq!(
            options
                .get_socket()
                .map(|path| path.to_string_lossy().into_owned())
                .as_deref(),
            Some("/cloudsql/project:region:instance")
        );
    }

    #[test]
    fn connect_options_carry_config() {
        let config = DbConfig {
            host: "db.example.com".into(),
            port: 6543,
            name: "mpac_test".into(),
            user: "mpac".into(),
            password: Some("secret".into()),
            pool_size: 4,
        };

        let options = config.connect_options();

        assert_eq!(options.get_host(), "db.example.com");
        assert_eq!(options.get_port(), 6543);
        assert_eq!(options.get_database(), Some("mpac_test"));
        assert_eq!(options.get_username(), "mpac");
    }
}
