use pbkdf2::pbkdf2_hmac;
use rand::distr::{Alphanumeric, SampleString};
use sha2::{Sha256, Sha512};
use subtle::ConstantTimeEq;

const SALT_LENGTH: usize = 16;
const SCRYPT_KEY_LENGTH: usize = 64;
const DEFAULT_SCRYPT_N: u64 = 1 << 15;
const DEFAULT_SCRYPT_R: u32 = 8;
const DEFAULT_SCRYPT_P: u32 = 1;
const DEFAULT_PBKDF2_HASH: &str = "sha256";
const DEFAULT_PBKDF2_ITERATIONS: u32 = 1_000_000;

#[must_use]
pub fn verify(stored: &str, password: &str) -> bool {
    let mut parts = stored.splitn(3, '$');
    let (Some(method), Some(salt), Some(expected)) = (parts.next(), parts.next(), parts.next())
    else {
        return false;
    };
    derive(method, salt, password)
        .is_some_and(|actual| bool::from(actual.as_bytes().ct_eq(expected.as_bytes())))
}

#[must_use]
pub fn hash(password: &str) -> String {
    let salt = Alphanumeric.sample_string(&mut rand::rng(), SALT_LENGTH);
    let method = format!("scrypt:{DEFAULT_SCRYPT_N}:{DEFAULT_SCRYPT_R}:{DEFAULT_SCRYPT_P}");
    let derived = scrypt_hex(
        &salt,
        password,
        DEFAULT_SCRYPT_N,
        DEFAULT_SCRYPT_R,
        DEFAULT_SCRYPT_P,
    )
    .unwrap_or_default();
    format!("{method}${salt}${derived}")
}

fn derive(method: &str, salt: &str, password: &str) -> Option<String> {
    let mut parts = method.split(':');
    let name = parts.next()?;
    let args: Vec<&str> = parts.collect();
    match (name, args.as_slice()) {
        ("scrypt", []) => scrypt_hex(
            salt,
            password,
            DEFAULT_SCRYPT_N,
            DEFAULT_SCRYPT_R,
            DEFAULT_SCRYPT_P,
        ),
        ("scrypt", [n, r, p]) => scrypt_hex(
            salt,
            password,
            n.parse().ok()?,
            r.parse().ok()?,
            p.parse().ok()?,
        ),
        ("pbkdf2", []) => pbkdf2_hex(
            DEFAULT_PBKDF2_HASH,
            salt,
            password,
            DEFAULT_PBKDF2_ITERATIONS,
        ),
        ("pbkdf2", [hash_name]) => pbkdf2_hex(hash_name, salt, password, DEFAULT_PBKDF2_ITERATIONS),
        ("pbkdf2", [hash_name, iterations]) => {
            pbkdf2_hex(hash_name, salt, password, iterations.parse().ok()?)
        }
        _ => None,
    }
}

fn scrypt_hex(salt: &str, password: &str, n: u64, r: u32, p: u32) -> Option<String> {
    if !n.is_power_of_two() || n < 2 {
        return None;
    }
    let log_n = u8::try_from(n.trailing_zeros()).ok()?;
    let params = scrypt::Params::new(log_n, r, p).ok()?;
    let mut output = [0_u8; SCRYPT_KEY_LENGTH];
    scrypt::scrypt(password.as_bytes(), salt.as_bytes(), &params, &mut output).ok()?;
    Some(hex::encode(output))
}

fn pbkdf2_hex(hash_name: &str, salt: &str, password: &str, iterations: u32) -> Option<String> {
    match hash_name {
        "sha256" => {
            let mut output = [0_u8; 32];
            pbkdf2_hmac::<Sha256>(
                password.as_bytes(),
                salt.as_bytes(),
                iterations,
                &mut output,
            );
            Some(hex::encode(output))
        }
        "sha512" => {
            let mut output = [0_u8; 64];
            pbkdf2_hmac::<Sha512>(
                password.as_bytes(),
                salt.as_bytes(),
                iterations,
                &mut output,
            );
            Some(hex::encode(output))
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fixtures::{cases, field};

    #[test]
    fn verifies_werkzeug_hashes() {
        for case in cases("passwords") {
            let stored = field(case, "hash");
            let password = field(case, "password");
            assert!(verify(stored, password), "{stored}");
            assert!(!verify(stored, &format!("{password}x")), "{stored}");
        }
    }

    #[test]
    fn hash_produces_werkzeug_default_format() {
        let stored = hash("hunter2");

        assert!(stored.starts_with("scrypt:32768:8:1$"), "{stored}");
        assert!(verify(&stored, "hunter2"));
        assert!(!verify(&stored, "hunter3"));
    }

    #[test]
    fn rejects_malformed_and_unsupported_hashes() {
        for stored in [
            "",
            "scrypt:32768:8:1",
            "scrypt:32768:8:1$salt",
            "scrypt:3:8:1$salt$00",
            "scrypt:32768:8$salt$00",
            "pbkdf2:md5:1000$salt$00",
            "plain$salt$hunter2",
        ] {
            assert!(!verify(stored, "hunter2"), "{stored}");
        }
    }

    #[test]
    fn rejects_dummy_login_hash() {
        let dummy = format!("scrypt:32768:8:1$bRT3PWGCcnSO0ETo${}", "0".repeat(128));

        assert!(!verify(&dummy, ""));
    }
}
