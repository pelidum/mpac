use std::env;

use mpac_db::DbConfig;
use sqlx::{Connection, Executor, PgConnection, PgPool};

fn server_config(name: &str, pool_size: u32) -> DbConfig {
    let var = |key: &str| {
        env::var(key).unwrap_or_else(|_| {
            panic!("{key} is unset; run this test through its rust_db_test wrapper")
        })
    };
    DbConfig {
        host: var("MPAC_TEST_PG_HOST"),
        port: var("MPAC_TEST_PG_PORT").parse().unwrap_or(5432),
        name: name.to_owned(),
        user: var("MPAC_TEST_PG_USER"),
        password: Some(var("MPAC_TEST_PG_PASSWORD")),
        pool_size,
    }
}

#[derive(Debug)]
pub struct TestDb {
    pub name: String,
    pub pool: PgPool,
}

impl TestDb {
    pub async fn create() -> Self {
        let name = format!("mpac_rust_{}", uuid::Uuid::new_v4().simple());
        let mut admin = PgConnection::connect_with(&server_config("postgres", 1).connect_options())
            .await
            .unwrap_or_else(|error| panic!("cannot reach test postgres: {error}"));
        admin
            .execute(sqlx::AssertSqlSafe(format!("CREATE DATABASE \"{name}\"")))
            .await
            .unwrap_or_else(|error| panic!("cannot create {name}: {error}"));
        let pool = mpac_db::connect(&server_config(&name, 16))
            .await
            .unwrap_or_else(|error| panic!("cannot connect to {name}: {error}"));
        mpac_db::migrate(&pool)
            .await
            .unwrap_or_else(|error| panic!("cannot migrate {name}: {error}"));
        Self { name, pool }
    }

    pub async fn pool(&self, size: u32) -> PgPool {
        mpac_db::connect(&server_config(&self.name, size))
            .await
            .unwrap_or_else(|error| panic!("cannot connect to {}: {error}", self.name))
    }
}
