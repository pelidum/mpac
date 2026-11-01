use std::time::{Duration, SystemTime, UNIX_EPOCH};

use base64::Engine as _;
use base64::engine::general_purpose::STANDARD as BASE64;
use ring::hmac;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use url::Url;

use crate::SecretError;

const TIMEOUT: Duration = Duration::from_secs(30);
const AWS_CONTAINER_CREDENTIALS_HOST: &str = "http://169.254.170.2";
const AWS_CONTENT_TYPE: &str = "application/x-amz-json-1.1";
const CONTEXT_KEY: &str = "backend_id";
const GCP_ENDPOINT: &str = "https://cloudkms.googleapis.com";
const GCP_TOKEN_URL: &str =
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token";

fn kms_error(message: impl std::fmt::Display) -> SecretError {
    SecretError::Kms(message.to_string())
}

fn http_client() -> Result<reqwest::Client, SecretError> {
    mpac_http::client(TIMEOUT).map_err(kms_error)
}

fn decode_field(reply: &Value, field: &str) -> Result<Vec<u8>, SecretError> {
    let encoded = reply
        .get(field)
        .and_then(Value::as_str)
        .ok_or_else(|| kms_error(format!("reply has no {field}")))?;
    BASE64
        .decode(encoded)
        .map_err(|_| kms_error(format!("{field} is not base64")))
}

async fn read_reply(
    operation: &str,
    response: reqwest::Response,
    error_code: impl Fn(&Value) -> Option<&str>,
) -> Result<Value, SecretError> {
    let status = response.status();
    let reply: Result<Value, _> = response.json().await;
    if !status.is_success() {
        let code = reply
            .as_ref()
            .ok()
            .and_then(error_code)
            .unwrap_or("no error code");
        return Err(kms_error(format!(
            "{operation} returned HTTP {status}: {code}"
        )));
    }
    reply.map_err(|error| kms_error(error.without_url()))
}

struct AwsCredentials {
    access_key: String,
    secret_key: String,
    session_token: Option<String>,
}

enum AwsCredentialSource {
    Fixed(AwsCredentials),
    Container(String),
}

pub(crate) struct AwsKms {
    http: reqwest::Client,
    key_arn: String,
    region: String,
    endpoint: Url,
    credentials: AwsCredentialSource,
}

fn nonempty_env(name: &str) -> Option<String> {
    std::env::var(name).ok().filter(|value| !value.is_empty())
}

fn aws_credential_source() -> Result<AwsCredentialSource, SecretError> {
    if let (Some(access_key), Some(secret_key)) = (
        nonempty_env("AWS_ACCESS_KEY_ID"),
        nonempty_env("AWS_SECRET_ACCESS_KEY"),
    ) {
        return Ok(AwsCredentialSource::Fixed(AwsCredentials {
            access_key,
            secret_key,
            session_token: nonempty_env("AWS_SESSION_TOKEN"),
        }));
    }
    nonempty_env("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
        .map(|path| {
            AwsCredentialSource::Container(format!("{AWS_CONTAINER_CREDENTIALS_HOST}{path}"))
        })
        .ok_or_else(|| {
            kms_error("no AWS credentials: use an ECS task role or set AWS_ACCESS_KEY_ID")
        })
}

fn hmac_sha256(key: &[u8], message: &str) -> hmac::Tag {
    hmac::sign(&hmac::Key::new(hmac::HMAC_SHA256, key), message.as_bytes())
}

struct SigningScope<'a> {
    datetime: &'a str,
    region: &'a str,
    service: &'a str,
}

impl SigningScope<'_> {
    fn date(&self) -> &str {
        &self.datetime[..8]
    }

    fn credential_scope(&self) -> String {
        format!(
            "{}/{}/{}/aws4_request",
            self.date(),
            self.region,
            self.service
        )
    }
}

fn sigv4_signature(secret_key: &str, scope: &SigningScope<'_>, canonical_request: &str) -> String {
    let string_to_sign = format!(
        "AWS4-HMAC-SHA256\n{}\n{}\n{}",
        scope.datetime,
        scope.credential_scope(),
        hex::encode(Sha256::digest(canonical_request.as_bytes()))
    );
    let date_key = hmac_sha256(format!("AWS4{secret_key}").as_bytes(), scope.date());
    let region_key = hmac_sha256(date_key.as_ref(), scope.region);
    let service_key = hmac_sha256(region_key.as_ref(), scope.service);
    let signing_key = hmac_sha256(service_key.as_ref(), "aws4_request");
    hex::encode(hmac_sha256(signing_key.as_ref(), &string_to_sign).as_ref())
}

fn kms_authorization(
    credentials: &AwsCredentials,
    scope: &SigningScope<'_>,
    host: &str,
    target: &str,
    body: &str,
) -> String {
    let token_header = credentials
        .session_token
        .as_ref()
        .map_or_else(String::new, |token| {
            format!("x-amz-security-token:{token}\n")
        });
    let signed_headers = if credentials.session_token.is_some() {
        "content-type;host;x-amz-date;x-amz-security-token;x-amz-target"
    } else {
        "content-type;host;x-amz-date;x-amz-target"
    };
    let canonical_request = format!(
        "POST\n/\n\ncontent-type:{AWS_CONTENT_TYPE}\nhost:{host}\nx-amz-date:{}\n{token_header}x-amz-target:{target}\n\n{signed_headers}\n{}",
        scope.datetime,
        hex::encode(Sha256::digest(body.as_bytes()))
    );
    format!(
        "AWS4-HMAC-SHA256 Credential={}/{}, SignedHeaders={signed_headers}, Signature={}",
        credentials.access_key,
        scope.credential_scope(),
        sigv4_signature(&credentials.secret_key, scope, &canonical_request)
    )
}

fn amz_datetime(unix_seconds: u64) -> String {
    let days = unix_seconds / 86_400;
    let seconds = unix_seconds % 86_400;
    let shifted = days + 719_468;
    let era = shifted / 146_097;
    let day_of_era = shifted % 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_index = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_index + 2) / 5 + 1;
    let month = if month_index < 10 {
        month_index + 3
    } else {
        month_index - 9
    };
    let year = year_of_era + era * 400 + u64::from(month <= 2);
    format!(
        "{year:04}{month:02}{day:02}T{:02}{:02}{:02}Z",
        seconds / 3_600,
        seconds / 60 % 60,
        seconds % 60
    )
}

impl AwsKms {
    pub(crate) fn open(key_arn: &str) -> Result<Self, SecretError> {
        let region = aws_arn_region(key_arn)?;
        let endpoint = nonempty_env("AWS_ENDPOINT_URL_KMS")
            .unwrap_or_else(|| format!("https://kms.{region}.amazonaws.com"));
        Ok(Self {
            http: http_client()?,
            key_arn: key_arn.to_owned(),
            region,
            endpoint: Url::parse(&endpoint).map_err(|error| kms_error(error.to_string()))?,
            credentials: aws_credential_source()?,
        })
    }

    async fn credentials(&self) -> Result<AwsCredentials, SecretError> {
        let url = match &self.credentials {
            AwsCredentialSource::Fixed(credentials) => {
                return Ok(AwsCredentials {
                    access_key: credentials.access_key.clone(),
                    secret_key: credentials.secret_key.clone(),
                    session_token: credentials.session_token.clone(),
                });
            }
            AwsCredentialSource::Container(url) => url,
        };
        let reply: Value = self
            .http
            .get(url)
            .send()
            .await
            .and_then(reqwest::Response::error_for_status)
            .map_err(|error| kms_error(error.without_url()))?
            .json()
            .await
            .map_err(|error| kms_error(error.without_url()))?;
        let field = |name: &str| {
            reply
                .get(name)
                .and_then(Value::as_str)
                .map(str::to_owned)
                .ok_or_else(|| kms_error(format!("container credentials have no {name}")))
        };
        Ok(AwsCredentials {
            access_key: field("AccessKeyId")?,
            secret_key: field("SecretAccessKey")?,
            session_token: Some(field("Token")?),
        })
    }

    async fn call(&self, operation: &str, body: &Value) -> Result<Value, SecretError> {
        let credentials = self.credentials().await?;
        let body = body.to_string();
        let target = format!("TrentService.{operation}");
        let host = self.endpoint.authority().to_owned();
        let unix_seconds = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(kms_error)?
            .as_secs();
        let datetime = amz_datetime(unix_seconds);
        let scope = SigningScope {
            datetime: &datetime,
            region: &self.region,
            service: "kms",
        };
        let authorization = kms_authorization(&credentials, &scope, &host, &target, &body);
        let mut request = self
            .http
            .post(self.endpoint.clone())
            .header("Content-Type", AWS_CONTENT_TYPE)
            .header("X-Amz-Date", &datetime)
            .header("X-Amz-Target", &target)
            .header("Authorization", authorization);
        if let Some(token) = &credentials.session_token {
            request = request.header("X-Amz-Security-Token", token);
        }
        let response = request
            .body(body)
            .send()
            .await
            .map_err(|error| kms_error(error.without_url()))?;
        read_reply(operation, response, |reply| {
            reply.get("__type").and_then(Value::as_str)
        })
        .await
    }

    pub(crate) async fn encrypt(
        &self,
        associated: &str,
        plaintext: &[u8],
    ) -> Result<Vec<u8>, SecretError> {
        let body = json!({
            "KeyId": self.key_arn,
            "Plaintext": BASE64.encode(plaintext),
            "EncryptionContext": {CONTEXT_KEY: associated},
        });
        decode_field(&self.call("Encrypt", &body).await?, "CiphertextBlob")
    }

    pub(crate) async fn decrypt(
        &self,
        associated: &str,
        ciphertext: &[u8],
    ) -> Result<Vec<u8>, SecretError> {
        let body = json!({
            "KeyId": self.key_arn,
            "CiphertextBlob": BASE64.encode(ciphertext),
            "EncryptionContext": {CONTEXT_KEY: associated},
        });
        decode_field(&self.call("Decrypt", &body).await?, "Plaintext")
    }
}

fn aws_arn_region(key_arn: &str) -> Result<String, SecretError> {
    let parts: Vec<&str> = key_arn.splitn(6, ':').collect();
    match parts.as_slice() {
        ["arn", _, "kms", region, _, resource]
            if !region.is_empty()
                && (resource.starts_with("key/") || resource.starts_with("alias/")) =>
        {
            Ok((*region).to_owned())
        }
        _ => Err(SecretError::InvalidUrl(
            "aws-kms url must name a key or alias ARN".into(),
        )),
    }
}

pub(crate) struct GcpKms {
    http: reqwest::Client,
    key_name: String,
    endpoint: String,
    token_url: String,
    fixed_token: Option<String>,
}

impl GcpKms {
    pub(crate) fn open(key_name: &str) -> Result<Self, SecretError> {
        let parts: Vec<&str> = key_name.split('/').collect();
        if !matches!(
            parts.as_slice(),
            [
                "projects",
                _,
                "locations",
                _,
                "keyRings",
                _,
                "cryptoKeys",
                _
            ]
        ) {
            return Err(SecretError::InvalidUrl(
                "gcp-kms url must be projects/P/locations/L/keyRings/R/cryptoKeys/K".into(),
            ));
        }
        Ok(Self {
            http: http_client()?,
            key_name: key_name.to_owned(),
            endpoint: GCP_ENDPOINT.to_owned(),
            token_url: GCP_TOKEN_URL.to_owned(),
            fixed_token: nonempty_env("GOOGLE_OAUTH_ACCESS_TOKEN"),
        })
    }

    async fn token(&self) -> Result<String, SecretError> {
        if let Some(token) = &self.fixed_token {
            return Ok(token.clone());
        }
        let reply: Value = self
            .http
            .get(&self.token_url)
            .header("Metadata-Flavor", "Google")
            .send()
            .await
            .and_then(reqwest::Response::error_for_status)
            .map_err(|error| kms_error(error.without_url()))?
            .json()
            .await
            .map_err(|error| kms_error(error.without_url()))?;
        reply
            .get("access_token")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| kms_error("metadata reply has no access_token"))
    }

    async fn call(&self, operation: &str, body: &Value) -> Result<Value, SecretError> {
        let token = self.token().await?;
        let response = self
            .http
            .post(format!(
                "{}/v1/{}:{operation}",
                self.endpoint, self.key_name
            ))
            .bearer_auth(token)
            .json(body)
            .send()
            .await
            .map_err(|error| kms_error(error.without_url()))?;
        read_reply(operation, response, |reply| {
            reply.pointer("/error/status").and_then(Value::as_str)
        })
        .await
    }

    pub(crate) async fn encrypt(
        &self,
        associated: &str,
        plaintext: &[u8],
    ) -> Result<Vec<u8>, SecretError> {
        let body = json!({
            "plaintext": BASE64.encode(plaintext),
            "additionalAuthenticatedData": BASE64.encode(associated),
        });
        decode_field(&self.call("encrypt", &body).await?, "ciphertext")
    }

    pub(crate) async fn decrypt(
        &self,
        associated: &str,
        ciphertext: &[u8],
    ) -> Result<Vec<u8>, SecretError> {
        let body = json!({
            "ciphertext": BASE64.encode(ciphertext),
            "additionalAuthenticatedData": BASE64.encode(associated),
        });
        decode_field(&self.call("decrypt", &body).await?, "plaintext")
    }
}

#[cfg(test)]
mod tests {
    use wiremock::matchers::{header, header_exists, method, path};
    use wiremock::{Mock, MockServer, Request, ResponseTemplate};

    use super::*;

    const ARN: &str = "arn:aws:kms:us-east-1:111122223333:key/abc";

    fn fixed_credentials(token: Option<&str>) -> AwsCredentials {
        AwsCredentials {
            access_key: "AKIDEXAMPLE".into(),
            secret_key: "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY".into(),
            session_token: token.map(str::to_owned),
        }
    }

    fn aws_kms(server: &MockServer, credentials: AwsCredentialSource) -> AwsKms {
        AwsKms {
            http: http_client().unwrap(),
            key_arn: ARN.into(),
            region: "us-east-1".into(),
            endpoint: Url::parse(&server.uri()).unwrap(),
            credentials,
        }
    }

    fn gcp_kms(server: &MockServer, fixed_token: Option<&str>) -> GcpKms {
        GcpKms {
            http: http_client().unwrap(),
            key_name: "projects/p/locations/l/keyRings/r/cryptoKeys/k".into(),
            endpoint: server.uri(),
            token_url: format!("{}/token", server.uri()),
            fixed_token: fixed_token.map(str::to_owned),
        }
    }

    #[test]
    fn signature_matches_the_aws_documentation_example() {
        let scope = SigningScope {
            datetime: "20150830T123600Z",
            region: "us-east-1",
            service: "iam",
        };
        let canonical_request = "GET\n/\nAction=ListUsers&Version=2010-05-08\ncontent-type:application/x-www-form-urlencoded; charset=utf-8\nhost:iam.amazonaws.com\nx-amz-date:20150830T123600Z\n\ncontent-type;host;x-amz-date\ne3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

        assert_eq!(
            sigv4_signature(
                "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
                &scope,
                canonical_request
            ),
            "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
        );
    }

    #[test]
    fn authorization_matches_botocore() {
        let scope = SigningScope {
            datetime: "20261101T123456Z",
            region: "us-east-1",
            service: "kms",
        };
        let body = r#"{"KeyId":"arn:aws:kms:us-east-1:111122223333:key/abc","Plaintext":"AQID"}"#;

        assert_eq!(
            kms_authorization(
                &fixed_credentials(Some("SESSIONTOKEN")),
                &scope,
                "kms.us-east-1.amazonaws.com",
                "TrentService.Encrypt",
                body
            ),
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261101/us-east-1/kms/aws4_request, SignedHeaders=content-type;host;x-amz-date;x-amz-security-token;x-amz-target, Signature=7076403f3c638ff63188130d2bdebb36f1d612ff3cb5efdf953593f961b0187d"
        );
        assert_eq!(
            kms_authorization(
                &fixed_credentials(None),
                &scope,
                "kms.us-east-1.amazonaws.com",
                "TrentService.Encrypt",
                body
            ),
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20261101/us-east-1/kms/aws4_request, SignedHeaders=content-type;host;x-amz-date;x-amz-target, Signature=213ff3878f17d77df867feeb4f3393504248cbc7d0723a2550a55c5ae2f1ef59"
        );
    }

    #[test]
    fn datetime_formats_utc_including_a_leap_day() {
        assert_eq!(amz_datetime(1_793_536_496), "20261101T123456Z");
        assert_eq!(amz_datetime(1_709_164_800), "20240229T000000Z");
        assert_eq!(amz_datetime(0), "19700101T000000Z");
    }

    #[test]
    fn key_names_are_validated() {
        assert_eq!(aws_arn_region(ARN).unwrap(), "us-east-1");
        assert_eq!(
            aws_arn_region("arn:aws:kms:eu-west-1:1:alias/mpac").unwrap(),
            "eu-west-1"
        );
        assert!(aws_arn_region("arn:aws:s3:::bucket").is_err());
        assert!(aws_arn_region("arn:aws:kms::1:key/abc").is_err());
        assert!(GcpKms::open("projects/p/keyRings/r").is_err());
        assert!(GcpKms::open("projects/p/locations/l/keyRings/r/cryptoKeys/k").is_ok());
    }

    #[tokio::test]
    async fn aws_round_trip_binds_the_backend_and_uses_container_credentials() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/credentials"))
            .respond_with(ResponseTemplate::new(200).set_body_json(json!({
                "AccessKeyId": "ASIAEXAMPLE",
                "SecretAccessKey": "role-secret",
                "Token": "role-token",
            })))
            .expect(2)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(header("X-Amz-Target", "TrentService.Encrypt"))
            .and(header("Content-Type", AWS_CONTENT_TYPE))
            .and(header("X-Amz-Security-Token", "role-token"))
            .and(header_exists("Authorization"))
            .respond_with(|request: &Request| {
                let body: Value = serde_json::from_slice(&request.body).unwrap();
                assert_eq!(body["KeyId"], ARN);
                assert_eq!(body["EncryptionContext"]["backend_id"], "backend");
                ResponseTemplate::new(200)
                    .set_body_json(json!({"CiphertextBlob": body["Plaintext"]}))
            })
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(header("X-Amz-Target", "TrentService.Decrypt"))
            .respond_with(|request: &Request| {
                let body: Value = serde_json::from_slice(&request.body).unwrap();
                assert_eq!(body["EncryptionContext"]["backend_id"], "backend");
                ResponseTemplate::new(200)
                    .set_body_json(json!({"Plaintext": body["CiphertextBlob"]}))
            })
            .mount(&server)
            .await;
        let kms = aws_kms(
            &server,
            AwsCredentialSource::Container(format!("{}/credentials", server.uri())),
        );

        let wrapped = kms.encrypt("backend", b"data-key").await.unwrap();

        assert_eq!(kms.decrypt("backend", &wrapped).await.unwrap(), b"data-key");
    }

    #[tokio::test]
    async fn aws_errors_name_the_code_without_credentials_or_plaintext() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(400).set_body_json(json!({
                "__type": "AccessDeniedException",
                "Message": "denied",
            })))
            .mount(&server)
            .await;
        let kms = aws_kms(
            &server,
            AwsCredentialSource::Fixed(fixed_credentials(Some("SESSIONTOKEN"))),
        );

        let error = kms.encrypt("backend", b"data-key").await.unwrap_err();
        let text = format!("{error} {error:?}");

        assert!(text.contains("AccessDeniedException"));
        assert!(!text.contains("EXAMPLEKEY"));
        assert!(!text.contains("SESSIONTOKEN"));
        assert!(!text.contains(&BASE64.encode(b"data-key")));
    }

    #[tokio::test]
    async fn gcp_round_trip_binds_the_backend_and_uses_the_metadata_token() {
        let server = MockServer::start().await;
        let key_path = "/v1/projects/p/locations/l/keyRings/r/cryptoKeys/k";
        Mock::given(method("GET"))
            .and(path("/token"))
            .and(header("Metadata-Flavor", "Google"))
            .respond_with(
                ResponseTemplate::new(200).set_body_json(json!({"access_token": "gcp-token"})),
            )
            .expect(2)
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path(format!("{key_path}:encrypt")))
            .and(header("Authorization", "Bearer gcp-token"))
            .respond_with(|request: &Request| {
                let body: Value = serde_json::from_slice(&request.body).unwrap();
                assert_eq!(
                    body["additionalAuthenticatedData"],
                    BASE64.encode("backend")
                );
                ResponseTemplate::new(200).set_body_json(json!({"ciphertext": body["plaintext"]}))
            })
            .mount(&server)
            .await;
        Mock::given(method("POST"))
            .and(path(format!("{key_path}:decrypt")))
            .and(header("Authorization", "Bearer gcp-token"))
            .respond_with(|request: &Request| {
                let body: Value = serde_json::from_slice(&request.body).unwrap();
                assert_eq!(
                    body["additionalAuthenticatedData"],
                    BASE64.encode("backend")
                );
                ResponseTemplate::new(200).set_body_json(json!({"plaintext": body["ciphertext"]}))
            })
            .mount(&server)
            .await;
        let kms = gcp_kms(&server, None);

        let wrapped = kms.encrypt("backend", b"data-key").await.unwrap();

        assert_eq!(kms.decrypt("backend", &wrapped).await.unwrap(), b"data-key");
    }

    #[tokio::test]
    async fn gcp_errors_name_the_status_without_the_token() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .respond_with(ResponseTemplate::new(403).set_body_json(json!({
                "error": {"status": "PERMISSION_DENIED", "message": "denied"},
            })))
            .mount(&server)
            .await;
        let kms = gcp_kms(&server, Some("super-secret-token"));

        let error = kms.encrypt("backend", b"data-key").await.unwrap_err();
        let text = format!("{error} {error:?}");

        assert!(text.contains("PERMISSION_DENIED"));
        assert!(!text.contains("super-secret-token"));
    }
}
