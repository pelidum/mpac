use std::sync::LazyLock;

use regex::Regex;
use serde_json::Value;

use crate::difflib::closest_match;
use crate::pystr::{lower, splitlines, str_of, strip};

const CLOSE_MATCH_CUTOFF: f64 = 0.6;
const JSON_ANSWER_KEYS: [&str; 9] = [
    "label",
    "answer",
    "choice",
    "prediction",
    "response",
    "output",
    "result",
    "category",
    "class",
];

fn regex(pattern: &str) -> Regex {
    Regex::new(pattern).unwrap_or_else(|error| panic!("invalid built-in regex {pattern}: {error}"))
}

static THINK: LazyLock<Regex> = LazyLock::new(|| regex(r"(?is)<think>.*?</think>"));
static EDGE_QUOTE: LazyLock<Regex> = LazyLock::new(|| regex(r#"^["']|["']$"#));
static FENCE_OPEN: LazyLock<Regex> = LazyLock::new(|| regex(r"^```\w*\n"));
static FENCE_CLOSE: LazyLock<Regex> = LazyLock::new(|| regex(r"\n```$"));
static INLINE_FENCE: LazyLock<Regex> = LazyLock::new(|| regex(r"^```(.+?)```$"));
static JSON_FENCE: LazyLock<Regex> = LazyLock::new(|| regex(r"(?s)^```(?:json)?\s*|\s*```$"));

fn match_choice(
    candidate: &str,
    choices: &[String],
    sanitized: &[String],
    longest_first: &[usize],
) -> Option<String> {
    let low = lower(candidate);
    let low = strip(&low);
    if let Some(&i) = longest_first.iter().find(|&&i| low == sanitized[i]) {
        return Some(choices[i].clone());
    }
    if let Some(&i) = longest_first
        .iter()
        .find(|&&i| low.starts_with(sanitized[i].as_str()) || low.ends_with(sanitized[i].as_str()))
    {
        return Some(choices[i].clone());
    }
    let contained: Vec<usize> = (0..sanitized.len())
        .filter(|&i| low.contains(sanitized[i].as_str()))
        .collect();
    if let [only] = contained.as_slice() {
        return Some(choices[*only].clone());
    }
    let close = closest_match(low, sanitized, CLOSE_MATCH_CUTOFF)?;
    let index = sanitized.iter().position(|s| s == close)?;
    Some(choices[index].clone())
}

fn json_candidate(text: &str) -> String {
    let unfenced = JSON_FENCE.replace_all(strip(text), "");
    let Ok(Value::Object(object)) = serde_json::from_str::<Value>(&unfenced) else {
        return String::new();
    };
    for key in JSON_ANSWER_KEYS {
        if let Some(value) = object.get(key).filter(|v| !v.is_null()) {
            return strip(&str_of(value)).to_owned();
        }
    }
    object
        .values()
        .find_map(Value::as_str)
        .map(|text| strip(text).to_owned())
        .unwrap_or_default()
}

pub fn validate_answer(raw: &str, choices: &[String]) -> String {
    let stripped = THINK.replace_all(raw, "");
    let stripped = strip(&stripped);
    let stripped = EDGE_QUOTE.replace_all(stripped, "");
    let stripped = strip(&stripped).to_owned();
    let stripped = FENCE_OPEN.replace(&stripped, "").into_owned();
    let stripped = FENCE_CLOSE.replace(&stripped, "").into_owned();
    let stripped = INLINE_FENCE.replace(&stripped, "$1").into_owned();
    let stripped = strip(&stripped).to_owned();

    let sanitized: Vec<String> = choices.iter().map(|c| lower(strip(c))).collect();
    let mut longest_first: Vec<usize> = (0..sanitized.len()).collect();
    longest_first.sort_by_key(|&i| std::cmp::Reverse(sanitized[i].chars().count()));

    let first_line = splitlines(&stripped)
        .into_iter()
        .map(strip)
        .find(|line| !line.is_empty())
        .unwrap_or(&stripped)
        .to_owned();

    let candidates = [
        first_line.clone(),
        json_candidate(&first_line),
        json_candidate(&stripped),
        stripped.clone(),
    ];
    for candidate in candidates.iter().filter(|c| !c.is_empty()) {
        if let Some(answer) = match_choice(candidate, choices, &sanitized, &longest_first) {
            return answer;
        }
    }
    tracing::warn!(first_line = %first_line.chars().take(120).collect::<String>(), "no answer choice matched");
    String::new()
}

#[cfg(test)]
mod tests {
    use serde_json::Value;

    use super::*;

    const FIXTURES: &str = include_str!("../testdata/inference_fixtures.json");

    #[test]
    fn matches_python_validate_answer_for_every_fixture() {
        let fixtures: Value = serde_json::from_str(FIXTURES).unwrap();
        let cases = fixtures["validate_answer"].as_array().unwrap();
        assert!(cases.len() > 400);
        let mut mismatches = Vec::new();
        for case in cases {
            let raw = case["raw"].as_str().unwrap();
            let choices: Vec<String> = serde_json::from_value(case["choices"].clone()).unwrap();
            let expected = case["answer"].as_str().unwrap();
            let actual = validate_answer(raw, &choices);
            if actual != expected {
                mismatches.push((raw.to_owned(), choices, expected.to_owned(), actual));
            }
        }
        assert!(mismatches.is_empty(), "{mismatches:#?}");
    }
}
