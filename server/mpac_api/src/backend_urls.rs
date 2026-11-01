use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

use url::{Host, Url};

const PRIVATE_V4: [(Ipv4Addr, u8); 14] = [
    (Ipv4Addr::UNSPECIFIED, 8),
    (Ipv4Addr::new(10, 0, 0, 0), 8),
    (Ipv4Addr::new(127, 0, 0, 0), 8),
    (Ipv4Addr::new(169, 254, 0, 0), 16),
    (Ipv4Addr::new(172, 16, 0, 0), 12),
    (Ipv4Addr::new(192, 0, 0, 0), 29),
    (Ipv4Addr::new(192, 0, 0, 170), 31),
    (Ipv4Addr::new(192, 0, 2, 0), 24),
    (Ipv4Addr::new(192, 168, 0, 0), 16),
    (Ipv4Addr::new(198, 18, 0, 0), 15),
    (Ipv4Addr::new(198, 51, 100, 0), 24),
    (Ipv4Addr::new(203, 0, 113, 0), 24),
    (Ipv4Addr::new(240, 0, 0, 0), 4),
    (Ipv4Addr::BROADCAST, 32),
];

const PRIVATE_V6: [(Ipv6Addr, u8); 10] = [
    (Ipv6Addr::LOCALHOST, 128),
    (Ipv6Addr::UNSPECIFIED, 128),
    (Ipv6Addr::new(0, 0, 0, 0, 0, 0xffff, 0, 0), 96),
    (Ipv6Addr::new(0x100, 0, 0, 0, 0, 0, 0, 0), 64),
    (Ipv6Addr::new(0x2001, 0, 0, 0, 0, 0, 0, 0), 23),
    (Ipv6Addr::new(0x2001, 2, 0, 0, 0, 0, 0, 0), 48),
    (Ipv6Addr::new(0x2001, 0xdb8, 0, 0, 0, 0, 0, 0), 32),
    (Ipv6Addr::new(0x2001, 0x10, 0, 0, 0, 0, 0, 0), 28),
    (Ipv6Addr::new(0xfc00, 0, 0, 0, 0, 0, 0, 0), 7),
    (Ipv6Addr::new(0xfe80, 0, 0, 0, 0, 0, 0, 0), 10),
];

fn in_v4(addr: Ipv4Addr, (network, prefix): (Ipv4Addr, u8)) -> bool {
    let mask = u32::MAX.checked_shl(32 - u32::from(prefix)).unwrap_or(0);
    u32::from(addr) & mask == u32::from(network) & mask
}

fn in_v6(addr: Ipv6Addr, (network, prefix): (Ipv6Addr, u8)) -> bool {
    let mask = u128::MAX.checked_shl(128 - u32::from(prefix)).unwrap_or(0);
    u128::from(addr) & mask == u128::from(network) & mask
}

pub fn is_private(addr: IpAddr) -> bool {
    match addr {
        IpAddr::V4(v4) => PRIVATE_V4.iter().any(|range| in_v4(v4, *range)),
        IpAddr::V6(v6) => PRIVATE_V6.iter().any(|range| in_v6(v6, *range)),
    }
}

fn private_error(addr: IpAddr, verb: &str) -> String {
    format!("Backend URL {verb} private address: {addr} (mark the backend as local to allow this)")
}

pub async fn validate(url: &str) -> Result<(), String> {
    let parsed = Url::parse(url).map_err(|error| format!("Invalid URL: {error}"))?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err(format!("Unsupported URL scheme: {}", parsed.scheme()));
    }
    let literal = match parsed.host() {
        None => return Err("URL has no hostname".into()),
        Some(Host::Ipv4(v4)) => Some(IpAddr::V4(v4)),
        Some(Host::Ipv6(v6)) => Some(IpAddr::V6(v6)),
        Some(Host::Domain(_)) => None,
    };
    if let Some(addr) = literal {
        return if is_private(addr) {
            Err(private_error(addr, "points to"))
        } else {
            Ok(())
        };
    }
    let host = parsed.host_str().unwrap_or_default();
    let Ok(resolved) = tokio::net::lookup_host((host, 0)).await else {
        return Ok(());
    };
    for socket in resolved {
        if is_private(socket.ip()) {
            return Err(private_error(socket.ip(), "resolves to"));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn private_ranges_match_python_ipaddress() {
        for private in [
            "10.1.2.3",
            "127.0.0.1",
            "169.254.1.1",
            "172.31.0.1",
            "192.168.1.1",
            "0.1.2.3",
            "::1",
            "fe80::1",
            "fd00::1",
            "::ffff:10.0.0.1",
            "2001:db8::1",
        ] {
            assert!(is_private(private.parse().unwrap()), "{private}");
        }
        for public in ["8.8.8.8", "100.64.0.1", "172.32.0.1", "2606:4700::1111"] {
            assert!(!is_private(public.parse().unwrap()), "{public}");
        }
    }

    #[tokio::test]
    async fn validate_rejects_bad_schemes_and_private_literals() {
        assert!(validate("ftp://example.com").await.is_err());
        assert!(validate("http://127.0.0.1:8000/v1").await.is_err());
        assert!(validate("http://[::1]:8000/v1").await.is_err());
        assert!(validate("https://8.8.8.8/v1").await.is_ok());
        assert!(validate("http://localhost:11434/v1").await.is_err());
    }
}
