use clap::{Args, Parser, Subcommand};
use mpac_db::DbConfig;
use mpac_db::onboarding::AdminOnboarding;

#[derive(Debug, Parser)]
#[command(
    name = "mpac",
    version,
    propagate_version = true,
    about = "Multi-Provider Agent Consensus backend"
)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Debug, Subcommand)]
pub enum Command {
    Serve(ServeArgs),
    Worker(ServeArgs),
    Migrate(MigrateArgs),
    #[command(name = "rotate-secret-key")]
    RotateSecretKey(RotateSecretKeyArgs),
    Onboard(OnboardArgs),
}

const DEFAULT_MAX_MESSAGE_BYTES: usize = 50 * 1024 * 1024;
const DEFAULT_MAX_MESSAGE_BYTES_WITH_BLOBS: usize = 8 * 1024 * 1024;

#[derive(Debug, Args)]
pub struct BlobArgs {
    #[arg(long = "blob_url", env = "MPAC_BLOB_URL")]
    pub url: Option<String>,
    #[arg(long = "blob_public_endpoint", env = "MPAC_BLOB_PUBLIC_ENDPOINT")]
    pub public_endpoint: Option<String>,
}

impl BlobArgs {
    pub fn open(&self) -> Result<Option<mpac_blob::BlobStore>, mpac_blob::BlobError> {
        self.url
            .as_deref()
            .filter(|url| !url.is_empty())
            .map(|url| {
                mpac_blob::BlobStore::open(
                    url,
                    self.public_endpoint.as_deref().filter(|e| !e.is_empty()),
                )
            })
            .transpose()
    }
}

#[derive(Debug, Args)]
pub struct ServeArgs {
    #[arg(long, default_value = "0.0.0.0")]
    pub host: String,
    #[arg(long, default_value_t = 50051)]
    pub port: u16,
    #[command(flatten)]
    pub db: DbArgs,
    #[command(flatten)]
    pub onboarding: OptionalOnboarding,
    #[arg(long = "skip_migrate")]
    pub skip_migrate: bool,
    #[arg(long = "max_message_size", env = "MPAC_MAX_MESSAGE_SIZE")]
    pub max_message_bytes: Option<usize>,
    #[arg(
        long = "max_attachment_size",
        env = "MPAC_MAX_ATTACHMENT_SIZE",
        default_value_t = mpac_api::attachments::DEFAULT_MAX_UPLOAD_BYTES
    )]
    pub max_attachment_bytes: u64,
    #[command(flatten)]
    pub blobs: BlobArgs,
    #[arg(
        long = "enable_reflection",
        env = "MPAC_ENABLE_REFLECTION",
        value_parser = parse_flag,
        default_value = "false"
    )]
    pub reflection: bool,
    #[arg(long = "jwt_secret", env = "MPAC_JWT_SECRET", hide_env_values = true)]
    pub jwt_secret: Option<String>,
    #[arg(
        long = "secret_key_url",
        env = "MPAC_SECRET_KEY_URL",
        hide_env_values = true
    )]
    pub secret_key_url: Option<String>,
    #[arg(
        long,
        env = "MPAC_ROLES",
        value_delimiter = ',',
        default_value = "api,worker"
    )]
    pub roles: Vec<Role>,
    #[arg(
        long = "worker_concurrency",
        env = "MPAC_WORKER_CONCURRENCY",
        default_value_t = 32
    )]
    pub worker_concurrency: usize,
}

impl ServeArgs {
    pub fn secrets(&self) -> Result<Option<mpac_secrets::SecretStore>, mpac_secrets::SecretError> {
        self.secret_key_url
            .as_deref()
            .filter(|url| !url.is_empty())
            .map(mpac_secrets::SecretStore::open)
            .transpose()
    }

    pub fn max_message_bytes(&self, blobs: bool) -> usize {
        self.max_message_bytes.unwrap_or(if blobs {
            DEFAULT_MAX_MESSAGE_BYTES_WITH_BLOBS
        } else {
            DEFAULT_MAX_MESSAGE_BYTES
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, clap::ValueEnum)]
pub enum Role {
    Api,
    Worker,
}

#[derive(Debug, Args)]
pub struct OptionalOnboarding {
    #[arg(
        id = "admin_onboarding_id",
        long = "admin_onboarding_id",
        env = "MPAC_ADMIN_ONBOARDING_ID"
    )]
    pub id: Option<String>,
    #[arg(
        id = "admin_onboarding_password",
        long = "admin_onboarding_password",
        env = "MPAC_ADMIN_ONBOARDING_PASSWORD",
        hide_env_values = true
    )]
    pub password: Option<String>,
    #[arg(
        id = "admin_onboarding_api_key",
        long = "admin_onboarding_api_key",
        env = "MPAC_ADMIN_API_KEY",
        hide_env_values = true
    )]
    pub api_key: Option<String>,
}

impl OptionalOnboarding {
    pub fn onboarding(&self) -> Option<AdminOnboarding> {
        let id = self.id.clone().filter(|id| !id.is_empty())?;
        Some(AdminOnboarding {
            id,
            password: self.password.clone().filter(|p| !p.is_empty()),
            api_key: self.api_key.clone().filter(|k| !k.is_empty()),
        })
    }
}

#[allow(clippy::unnecessary_wraps)]
fn parse_flag(value: &str) -> Result<bool, String> {
    Ok(matches!(value.to_ascii_lowercase().as_str(), "1" | "true"))
}

#[derive(Debug, Args)]
pub struct MigrateArgs {
    #[command(flatten)]
    pub db: DbArgs,
}

#[derive(Debug, Args)]
pub struct RotateSecretKeyArgs {
    #[command(flatten)]
    pub db: DbArgs,
    #[arg(
        long = "secret_key_url",
        env = "MPAC_SECRET_KEY_URL",
        hide_env_values = true
    )]
    pub secret_key_url: String,
    #[arg(long = "previous_secret_key_url")]
    pub previous_secret_key_url: String,
}

#[derive(Debug, Args)]
pub struct OnboardArgs {
    #[command(flatten)]
    pub db: DbArgs,
    #[command(flatten)]
    pub onboarding: OptionalOnboarding,
}

#[derive(Debug, Args)]
pub struct DbArgs {
    #[arg(id = "db_host", long = "db_host", default_value = "localhost")]
    pub host: String,
    #[arg(id = "db_port", long = "db_port", default_value_t = 5432)]
    pub port: u16,
    #[arg(id = "db_name", long = "db_name", default_value = "mpac")]
    pub name: String,
    #[arg(id = "db_user", long = "db_user", default_value = "postgres")]
    pub user: String,
    #[arg(
        id = "db_password",
        long = "db_password",
        env = "MPAC_DB_PASSWORD",
        hide_env_values = true
    )]
    pub password: Option<String>,
    #[arg(id = "db_pool_size", long = "db_pool_size", default_value_t = 10)]
    pub pool_size: u32,
}

impl DbArgs {
    pub fn config(&self) -> DbConfig {
        DbConfig {
            host: self.host.clone(),
            port: self.port,
            name: self.name.clone(),
            user: self.user.clone(),
            password: self.password.clone(),
            pool_size: self.pool_size,
        }
    }
}

#[cfg(test)]
mod tests {
    use clap::CommandFactory;

    use super::*;

    #[test]
    fn cli_definition_is_valid() {
        Cli::command().debug_assert();
    }

    #[test]
    fn migrate_accepts_python_server_db_flags() {
        let cli = Cli::try_parse_from([
            "mpac",
            "migrate",
            "--db_host=db.internal",
            "--db_port=6432",
            "--db_name=mpac_test",
            "--db_user=mpac",
            "--db_password=secret",
        ])
        .unwrap();

        let Command::Migrate(args) = cli.command else {
            panic!("expected migrate");
        };
        assert_eq!(
            args.db.config(),
            DbConfig {
                host: "db.internal".into(),
                port: 6432,
                name: "mpac_test".into(),
                user: "mpac".into(),
                password: Some("secret".into()),
                pool_size: 10,
            }
        );
    }

    #[test]
    fn onboard_accepts_python_server_onboarding_flags() {
        let cli = Cli::try_parse_from([
            "mpac",
            "onboard",
            "--admin_onboarding_id=admin@example.com",
            "--admin_onboarding_password=hunter2",
            "--admin_onboarding_api_key=",
        ])
        .unwrap();

        let Command::Onboard(args) = cli.command else {
            panic!("expected onboard");
        };
        assert_eq!(
            args.onboarding.onboarding(),
            Some(AdminOnboarding {
                id: "admin@example.com".into(),
                password: Some("hunter2".into()),
                api_key: None,
            })
        );
    }

    #[test]
    fn serve_accepts_terraform_command_lines() {
        let cli = Cli::try_parse_from([
            "mpac",
            "serve",
            "--db_host=/cloudsql/project:region:instance",
            "--db_user=mpac",
            "--db_pool_size=5",
            "--host=0.0.0.0",
            "--port=50051",
            "--admin_onboarding_id=admin@example.com",
        ])
        .unwrap();

        let Command::Serve(args) = cli.command else {
            panic!("expected serve");
        };
        assert_eq!(args.port, 50051);
        assert_eq!(args.db.config().pool_size, 5);
        assert_eq!(
            args.onboarding.onboarding().unwrap().id,
            "admin@example.com"
        );
        assert!(!args.skip_migrate);
    }

    #[test]
    fn version_is_available_on_every_subcommand() {
        let error = Cli::try_parse_from(["mpac", "serve", "--version"]).unwrap_err();

        assert_eq!(error.kind(), clap::error::ErrorKind::DisplayVersion);
    }

    #[test]
    fn roles_default_to_embedded_api_and_worker() {
        let Command::Serve(args) = Cli::try_parse_from(["mpac", "serve"]).unwrap().command else {
            panic!("expected serve");
        };
        assert_eq!(args.roles, vec![Role::Api, Role::Worker]);

        let Command::Serve(args) = Cli::try_parse_from(["mpac", "serve", "--roles=worker"])
            .unwrap()
            .command
        else {
            panic!("expected serve");
        };
        assert_eq!(args.roles, vec![Role::Worker]);
    }

    #[test]
    fn reflection_flag_matches_python_truthiness() {
        assert_eq!(parse_flag("TRUE"), Ok(true));
        assert_eq!(parse_flag("1"), Ok(true));
        assert_eq!(parse_flag("yes"), Ok(false));
    }
}
