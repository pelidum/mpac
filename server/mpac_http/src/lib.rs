use std::sync::Once;
use std::time::Duration;

pub const USER_AGENT: &str = concat!("MPAC/", env!("CARGO_PKG_VERSION"));

static CRYPTO_PROVIDER: Once = Once::new();

pub fn install_crypto_provider() {
    CRYPTO_PROVIDER.call_once(|| {
        rustls::crypto::ring::default_provider()
            .install_default()
            .ok();
    });
}

pub fn client(timeout: Duration) -> reqwest::Result<reqwest::Client> {
    install_crypto_provider();
    reqwest::Client::builder()
        .user_agent(USER_AGENT)
        .timeout(timeout)
        .build()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn client_builds_with_ring_provider() {
        assert!(client(Duration::from_secs(1)).is_ok());
        assert!(rustls::crypto::CryptoProvider::get_default().is_some());
    }
}
