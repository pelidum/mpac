use mpac_proto::{ChoiceProbability, TestRunAnswer, TokenLogprob};

use crate::pystr::{collapse_whitespace, lower, strip};

pub const MAX_ANSWER_TOKENS: usize = 32;
const LEADING_JUNK: [char; 8] = [' ', '\t', '\r', '\n', '"', '\'', '`', '*'];
const THINK_END: &str = "</think>";

fn norm_choice(text: &str) -> String {
    strip(&collapse_whitespace(&lower(text))).to_owned()
}

fn norm_prefix(text: &str) -> String {
    collapse_whitespace(&lower(text))
        .trim_start_matches(LEADING_JUNK)
        .to_owned()
}

fn completes(prefix: &str, choice: &str) -> bool {
    if !prefix.starts_with(choice) {
        return false;
    }
    prefix[choice.len()..]
        .chars()
        .next()
        .is_none_or(|next| !next.is_alphanumeric())
}

fn consistent(prefix: &str, choice: &str) -> bool {
    !prefix.is_empty() && (choice.starts_with(prefix) || completes(prefix, choice))
}

fn answer_start(tokens: &[TokenLogprob]) -> usize {
    let text: String = tokens.iter().map(|t| t.token.as_str()).collect();
    let lowered: Vec<char> = lower(&text).chars().collect();
    let needle: Vec<char> = THINK_END.chars().collect();
    let Some(index) = (0..=lowered.len().saturating_sub(needle.len()))
        .rev()
        .find(|&i| lowered.len() >= needle.len() && lowered[i..i + needle.len()] == needle[..])
    else {
        return 0;
    };
    let end = index + needle.len();
    let mut position = 0;
    for (i, token) in tokens.iter().enumerate() {
        position += token.token.chars().count();
        if position >= end {
            return i + 1;
        }
    }
    tokens.len()
}

#[derive(Debug, Default)]
struct Mass(Vec<(String, f64)>);

impl Mass {
    fn add(&mut self, choice: &str, amount: f64) {
        match self.0.iter_mut().find(|(c, _)| c == choice) {
            Some((_, mass)) => *mass += amount,
            None => self.0.push((choice.to_owned(), amount)),
        }
    }

    fn distribute(&mut self, targets: &[&String], amount: f64) {
        #[allow(clippy::cast_precision_loss)]
        let share = amount / targets.len() as f64;
        for target in targets {
            self.add(target, share);
        }
    }
}

#[allow(clippy::too_many_lines)]
pub fn set_answer_confidence(
    answer: &mut TestRunAnswer,
    tokens: Option<&[TokenLogprob]>,
    choices: &[String],
) {
    answer.confidence = None;
    answer.choice_probabilities.clear();
    let Some(tokens) = tokens.filter(|t| !t.is_empty()) else {
        return;
    };

    let mut norm_to_choice: Vec<(String, String)> = Vec::new();
    for choice in choices {
        let norm = norm_choice(choice);
        if !norm.is_empty() && !norm_to_choice.iter().any(|(n, _)| *n == norm) {
            norm_to_choice.push((norm, choice.clone()));
        }
    }
    let norm_choices: Vec<String> = norm_to_choice.iter().map(|(n, _)| n.clone()).collect();

    let mut mass = Mass::default();
    let mut prefix = String::new();
    let mut p_prefix = 1.0_f64;
    let mut completed: Option<String> = None;
    let mut pending: Option<String> = None;
    let mut settled: Vec<String> = Vec::new();
    let mut broke = false;

    for token in tokens[answer_start(tokens)..]
        .iter()
        .take(MAX_ANSWER_TOKENS)
    {
        let n_prefix = norm_prefix(&prefix);
        let alternatives: Vec<(&str, f64)> = token
            .top_logprobs
            .iter()
            .filter(|alt| alt.token != token.token)
            .map(|alt| (alt.token.as_str(), alt.logprob))
            .collect();
        let next_prefix = format!("{prefix}{}", token.token);
        let n_next = norm_prefix(&next_prefix);

        if let Some(waiting) = pending.clone() {
            let mut elsewhere = 0.0;
            for (text, logprob) in &alternatives {
                let extended = norm_prefix(&format!("{prefix}{text}"));
                let targets: Vec<&String> = norm_choices
                    .iter()
                    .filter(|c| **c != waiting && !settled.contains(c) && consistent(&extended, c))
                    .collect();
                if !targets.is_empty() {
                    let amount = p_prefix * logprob.exp();
                    mass.distribute(&targets, amount);
                    elsewhere += amount;
                }
            }
            let continues = norm_choices
                .iter()
                .any(|c| *c != waiting && !settled.contains(c) && consistent(&n_next, c));
            if continues {
                elsewhere += p_prefix * token.logprob.exp();
            }
            mass.add(&waiting, (p_prefix - elsewhere).max(0.0));
            if !continues {
                completed = Some(waiting);
                broke = true;
                break;
            }
            settled.push(waiting);
            pending = None;
        } else {
            for (text, logprob) in &alternatives {
                let extended = norm_prefix(&format!("{prefix}{text}"));
                if extended.is_empty() || extended == n_prefix {
                    continue;
                }
                let targets: Vec<&String> = norm_choices
                    .iter()
                    .filter(|c| !settled.contains(c) && consistent(&extended, c))
                    .collect();
                if !targets.is_empty() {
                    mass.distribute(&targets, p_prefix * logprob.exp());
                }
            }
        }

        prefix = next_prefix;
        p_prefix *= token.logprob.exp();
        if n_next.is_empty() {
            continue;
        }
        let live: Vec<&String> = norm_choices
            .iter()
            .filter(|c| !settled.contains(c) && consistent(&n_next, c))
            .collect();
        if live.is_empty() {
            broke = true;
            break;
        }
        let done: Vec<&String> = live
            .iter()
            .copied()
            .filter(|c| completes(&n_next, c))
            .collect();
        if let Some(best) = done.iter().copied().reduce(|best, c| {
            if c.chars().count() > best.chars().count() {
                c
            } else {
                best
            }
        }) {
            if live
                .iter()
                .any(|c| c.chars().count() > best.chars().count() && c.starts_with(best.as_str()))
            {
                pending = Some(best.clone());
                continue;
            }
            mass.add(best, p_prefix);
            completed = Some(best.clone());
            broke = true;
            break;
        }
    }
    if !broke && let Some(waiting) = pending {
        mass.add(&waiting, p_prefix);
        completed = Some(waiting);
    }

    let mut probabilities: Vec<(String, f64)> = mass
        .0
        .iter()
        .filter_map(|(norm, p)| {
            norm_to_choice
                .iter()
                .find(|(n, _)| n == norm)
                .map(|(_, choice)| (choice.clone(), p.min(1.0)))
        })
        .collect();
    probabilities.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
    #[allow(clippy::cast_possible_truncation)]
    for (choice, probability) in &probabilities {
        answer.choice_probabilities.push(ChoiceProbability {
            choice: choice.clone(),
            probability: *probability as f32,
        });
    }
    if let Some(done) = completed
        && done == norm_choice(&answer.answer)
        && let Some((_, choice)) = norm_to_choice.iter().find(|(n, _)| *n == done)
        && let Some((_, probability)) = probabilities.iter().find(|(c, _)| c == choice)
    {
        #[allow(clippy::cast_possible_truncation)]
        let confidence = *probability as f32;
        answer.confidence = Some(confidence);
    }
}

#[cfg(test)]
mod tests {
    use mpac_proto::token_logprob::TopLogprob;
    use serde_json::Value;

    use super::*;

    const FIXTURES: &str = include_str!("../testdata/inference_fixtures.json");

    fn tokens(case: &Value) -> Vec<TokenLogprob> {
        case["tokens"]
            .as_array()
            .unwrap()
            .iter()
            .map(|t| TokenLogprob {
                token: t["token"].as_str().unwrap().into(),
                logprob: t["logprob"].as_f64().unwrap(),
                top_logprobs: t["top_logprobs"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|a| TopLogprob {
                        token: a["token"].as_str().unwrap().into(),
                        logprob: a["logprob"].as_f64().unwrap(),
                    })
                    .collect(),
            })
            .collect()
    }

    #[test]
    fn matches_python_set_answer_confidence_for_every_fixture() {
        let fixtures: Value = serde_json::from_str(FIXTURES).unwrap();
        let cases = fixtures["confidence"].as_array().unwrap();
        assert!(cases.len() >= 300);
        let mut mismatches = Vec::new();
        let mut scored = 0;
        for (index, case) in cases.iter().enumerate() {
            let choices: Vec<String> = serde_json::from_value(case["choices"].clone()).unwrap();
            let mut answer = TestRunAnswer {
                answer: case["answer"].as_str().unwrap().into(),
                ..TestRunAnswer::default()
            };
            let tokens = tokens(case);
            set_answer_confidence(&mut answer, Some(&tokens), &choices);

            #[allow(clippy::cast_possible_truncation)]
            let expected_confidence = case["confidence"].as_f64().map(|c| c as f32);
            #[allow(clippy::cast_possible_truncation)]
            let expected: Vec<(String, f32)> = case["choice_probabilities"]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| {
                    (
                        p["choice"].as_str().unwrap().to_owned(),
                        p["probability"].as_f64().unwrap() as f32,
                    )
                })
                .collect();
            let actual: Vec<(String, f32)> = answer
                .choice_probabilities
                .iter()
                .map(|p| (p.choice.clone(), p.probability))
                .collect();
            let close = |a: f32, b: f32| (a - b).abs() <= 1e-6;
            let same_probabilities = expected.len() == actual.len()
                && expected
                    .iter()
                    .zip(&actual)
                    .all(|(e, a)| e.0 == a.0 && close(e.1, a.1));
            let same_confidence = match (expected_confidence, answer.confidence) {
                (None, None) => true,
                (Some(e), Some(a)) => close(e, a),
                _ => false,
            };
            if expected_confidence.is_some() {
                scored += 1;
            }
            if !same_probabilities || !same_confidence {
                mismatches.push((
                    index,
                    expected_confidence,
                    answer.confidence,
                    expected,
                    actual,
                ));
            }
        }
        assert!(mismatches.is_empty(), "{mismatches:#?}");
        assert!(scored > 100, "only {scored} fixtures produced a confidence");
    }

    #[test]
    fn no_tokens_clears_confidence() {
        let mut answer = TestRunAnswer {
            confidence: Some(0.5),
            choice_probabilities: vec![ChoiceProbability::default()],
            ..TestRunAnswer::default()
        };

        set_answer_confidence(&mut answer, None, &["A".into()]);

        assert_eq!(answer.confidence, None);
        assert_eq!(answer.choice_probabilities, Vec::new());
    }
}
