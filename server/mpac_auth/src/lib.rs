pub mod api_key;
pub mod jwt;
pub mod password;

#[cfg(test)]
pub(crate) mod fixtures {
    use std::sync::LazyLock;

    use serde_json::Value;

    pub static FIXTURES: LazyLock<Value> = LazyLock::new(|| {
        serde_json::from_str(include_str!("../testdata/auth_fixtures.json")).unwrap()
    });

    pub fn cases(section: &str) -> &'static [Value] {
        FIXTURES[section].as_array().unwrap()
    }

    pub fn field<'a>(case: &'a Value, name: &str) -> &'a str {
        case[name].as_str().unwrap()
    }
}
