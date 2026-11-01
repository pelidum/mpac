use sha2::{Digest, Sha256};

#[must_use]
pub fn hash(api_key: &str) -> String {
    hex::encode(Sha256::digest(api_key.as_bytes()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::fixtures::{cases, field};

    #[test]
    fn matches_python_sha256_hexdigest() {
        for case in cases("api_keys") {
            assert_eq!(hash(field(case, "key")), field(case, "hash"));
        }
    }
}
