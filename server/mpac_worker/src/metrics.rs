use std::collections::BTreeSet;

use mpac_proto::test_run::TestRunMetrics;
use mpac_proto::test_run_answer::Status;
use mpac_proto::{FileModality, TestItem, TestRunAnswer};

const LOCAL_POWER_WATTS: f64 = 300.0;
const LOCAL_KWH_PRICE: f64 = 0.14;
const LOCAL_HARDWARE_COST: f64 = 2000.0;
const LOCAL_HARDWARE_LIFESPAN_YEARS: f64 = 4.0;
const LOCAL_ANNUAL_USAGE_HOURS: f64 = 2000.0;

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Weighted {
    pub precision: f64,
    pub recall: f64,
    pub f1: f64,
}

#[allow(clippy::cast_precision_loss)]
pub fn weighted_prf(truth: &[String], predicted: &[String]) -> Weighted {
    let labels: BTreeSet<&String> = truth.iter().chain(predicted).collect();
    let (mut precision, mut recall, mut f1, mut support_total) = (0.0, 0.0, 0.0, 0.0);
    for label in labels {
        let support = truth.iter().filter(|t| *t == label).count() as f64;
        let predicted_count = predicted.iter().filter(|p| *p == label).count() as f64;
        let true_positives = truth
            .iter()
            .zip(predicted)
            .filter(|(t, p)| *t == label && *p == label)
            .count() as f64;
        let ratio = |num: f64, den: f64| if den > 0.0 { num / den } else { 0.0 };
        precision += ratio(true_positives, predicted_count) * support;
        recall += ratio(true_positives, support) * support;
        f1 += ratio(2.0 * true_positives, support + predicted_count) * support;
        support_total += support;
    }
    if support_total == 0.0 {
        return Weighted {
            precision: 0.0,
            recall: 0.0,
            f1: 0.0,
        };
    }
    Weighted {
        precision: precision / support_total,
        recall: recall / support_total,
        f1: f1 / support_total,
    }
}

fn median(sorted: &[f64]) -> f64 {
    let n = sorted.len();
    if n % 2 == 1 {
        sorted[n / 2]
    } else {
        f64::midpoint(sorted[n / 2 - 1], sorted[n / 2])
    }
}

#[allow(clippy::cast_precision_loss)]
fn exclusive_quantile(sorted: &[f64], n: usize, i: usize) -> f64 {
    let len = sorted.len();
    let m = len + 1;
    let j = (i * m / n).clamp(1, len - 1);
    let delta = (i * m) as f64 - (j * n) as f64;
    (sorted[j - 1] * (n as f64 - delta) + sorted[j] * delta) / n as f64
}

pub fn percentiles(values: &[f64]) -> (f64, f64, f64) {
    match values {
        [] => (0.0, 0.0, 0.0),
        [only] => (*only, *only, *only),
        _ => {
            let mut sorted = values.to_vec();
            sorted.sort_by(f64::total_cmp);
            (
                median(&sorted),
                exclusive_quantile(&sorted, 20, 19),
                exclusive_quantile(&sorted, 100, 99),
            )
        }
    }
}

pub fn grade(score: f64) -> &'static str {
    match score {
        s if s >= 0.95 => "⭐⭐⭐⭐⭐ High Quality",
        s if s >= 0.8 => "⭐⭐⭐⭐ OK Quality",
        s if s >= 0.7 => "⭐⭐⭐ Low Quality",
        s if s >= 0.5 => "⭐⭐ Very Low Quality",
        _ => "⭐ Garbage",
    }
}

#[allow(clippy::float_cmp)]
pub fn consensus(score: f64) -> &'static str {
    if score == 1.0 {
        "⭐⭐⭐⭐⭐ Unanimous consensus"
    } else if score >= 0.75 {
        "⭐⭐⭐⭐ Strong consensus"
    } else if score >= 0.6 {
        "⭐⭐⭐ Majority consensus"
    } else if score >= 0.4 {
        "⭐⭐ Weak consensus"
    } else {
        "⭐ No consensus"
    }
}

pub fn local_inference_cost(duration_seconds: f64) -> f64 {
    let energy = LOCAL_POWER_WATTS * duration_seconds / (1000.0 * 3600.0) * LOCAL_KWH_PRICE;
    let useful_seconds = LOCAL_HARDWARE_LIFESPAN_YEARS * LOCAL_ANNUAL_USAGE_HOURS * 3600.0;
    energy + LOCAL_HARDWARE_COST / useful_seconds * duration_seconds
}

#[allow(clippy::cast_possible_truncation)]
fn f32_of(value: f64) -> f32 {
    value as f32
}

fn is_request_success(answer: &TestRunAnswer) -> bool {
    matches!(
        Status::try_from(answer.status),
        Ok(Status::Ok | Status::Unspecified)
    )
}

fn apply_performance(metrics: &mut TestRunMetrics, answers: &[&TestRunAnswer]) {
    let ok: Vec<&&TestRunAnswer> = answers.iter().filter(|a| is_request_success(a)).collect();
    let positive = |pick: fn(&TestRunAnswer) -> f32| -> Vec<f64> {
        ok.iter()
            .map(|a| f64::from(pick(a)))
            .filter(|v| *v > 0.0)
            .collect()
    };
    let (p50, p95, p99) = percentiles(&positive(|a| a.task_duration));
    (
        metrics.median_task_duration,
        metrics.task_duration_p95,
        metrics.task_duration_p99,
    ) = (f32_of(p50), f32_of(p95), f32_of(p99));
    let ttfts = positive(|a| a.ttft);
    let (p50, p95, p99) = percentiles(&ttfts);
    (metrics.ttft_p50, metrics.ttft_p95, metrics.ttft_p99) =
        (f32_of(p50), f32_of(p95), f32_of(p99));
    let (p50, p95, _) = percentiles(&positive(|a| a.output_tps));
    (metrics.output_tps_p50, metrics.output_tps_p95) = (f32_of(p50), f32_of(p95));
    metrics.streamed = !ttfts.is_empty();
}

#[allow(clippy::cast_precision_loss)]
fn apply_confidence(metrics: &mut TestRunMetrics, answers: &[&TestRunAnswer]) {
    let scored: Vec<f64> = answers
        .iter()
        .filter(|a| is_request_success(a))
        .filter_map(|a| a.confidence.map(f64::from))
        .collect();
    metrics.mean_confidence = if scored.is_empty() {
        0.0
    } else {
        f32_of(scored.iter().sum::<f64>() / scored.len() as f64)
    };
}

#[derive(Debug, Clone)]
pub struct TimedAnswer {
    pub answer: TestRunAnswer,
    pub stored_at: f64,
}

#[derive(Debug, Clone)]
pub struct ResponderInput<'a> {
    pub responder_id: &'a str,
    pub model_id: &'a str,
    pub evaluation: bool,
    pub sample: &'a [TestItem],
    pub answers: &'a [&'a TimedAnswer],
    pub started_at: f64,
}

fn timestamp(seconds: f64) -> prost_types::Timestamp {
    #[allow(clippy::cast_possible_truncation)]
    let whole = seconds.floor() as i64;
    #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
    let nanos = ((seconds - seconds.floor()) * 1e9) as i32;
    prost_types::Timestamp {
        seconds: whole,
        nanos,
    }
}

#[allow(clippy::cast_precision_loss)]
fn apply_evaluation(
    metrics: &mut TestRunMetrics,
    input: &ResponderInput<'_>,
    answers: &[&TestRunAnswer],
) {
    let sample_size = input.sample.len();
    let ratio = |count: usize| {
        if sample_size > 0 {
            count as f64 / sample_size as f64
        } else {
            0.0
        }
    };
    let correct = answers.iter().filter(|a| a.is_correct).count();
    metrics.num_correct = i32::try_from(correct).unwrap_or(i32::MAX);
    metrics.num_positives =
        i32::try_from(input.sample.iter().filter(|i| i.is_relevant).count()).unwrap_or(i32::MAX);
    grade(ratio(correct)).clone_into(&mut metrics.simple_grade);
    let modalities: BTreeSet<i32> = answers.iter().map(|a| a.attachment_type).collect();
    for modality in modalities {
        let of_modality: Vec<&&TestRunAnswer> = answers
            .iter()
            .filter(|a| a.attachment_type == modality)
            .collect();
        let name = FileModality::try_from(modality)
            .map_or_else(|_| modality.to_string(), |m| m.as_str_name().to_owned());
        let correct = of_modality.iter().filter(|a| a.is_correct).count();
        metrics
            .modality_scores
            .insert(name, f32_of(correct as f64 / of_modality.len() as f64));
    }

    let mut truth = Vec::new();
    let mut predicted = Vec::new();
    let (mut tp, mut tn, mut fp, mut fn_) = (0, 0, 0, 0);
    let mut seen: Vec<&str> = Vec::new();
    for answer in answers.iter().rev() {
        if seen.contains(&answer.item_id.as_str()) {
            continue;
        }
        seen.push(&answer.item_id);
        let Some(item) = input.sample.iter().find(|i| i.id == answer.item_id) else {
            continue;
        };
        truth.push(item.answer.clone());
        predicted.push(answer.answer.clone());
        match (item.is_relevant, answer.is_correct) {
            (true, true) => tp += 1,
            (false, true) => tn += 1,
            (false, false) => fp += 1,
            (true, false) => fn_ += 1,
        }
    }
    let scores = weighted_prf(&truth, &predicted);
    (metrics.precision, metrics.recall, metrics.f1) = (scores.precision, scores.recall, scores.f1);
    (
        metrics.true_positives,
        metrics.true_negatives,
        metrics.false_positives,
        metrics.false_negatives,
    ) = (tp, tn, fp, fn_);
}

#[allow(clippy::cast_precision_loss, clippy::cast_possible_truncation)]
pub fn responder_metrics(input: &ResponderInput<'_>) -> TestRunMetrics {
    let answers: Vec<&TestRunAnswer> = input.answers.iter().map(|a| &a.answer).collect();
    let sample_size = input.sample.len();
    let ratio = |count: usize| {
        if sample_size > 0 {
            count as f64 / sample_size as f64
        } else {
            0.0
        }
    };
    let refusals = answers.iter().filter(|a| a.answer.is_empty()).count();
    let mut metrics = TestRunMetrics {
        responder_id: input.responder_id.to_owned(),
        model_id: input.model_id.to_owned(),
        num_refusals_errors: i32::try_from(refusals).unwrap_or(i32::MAX),
        total: i32::try_from(sample_size).unwrap_or(i32::MAX),
        refusal_error_rate: ratio(refusals),
        ..TestRunMetrics::default()
    };

    if input.evaluation {
        apply_evaluation(&mut metrics, input, &answers);
    }

    let first = input
        .answers
        .iter()
        .map(|a| a.stored_at)
        .fold(f64::INFINITY, f64::min);
    let last = input
        .answers
        .iter()
        .map(|a| a.stored_at)
        .fold(f64::NEG_INFINITY, f64::max);
    let wall = if last.is_finite() {
        last - input.started_at
    } else {
        0.0
    };
    let output_tokens: i64 = answers
        .iter()
        .map(|a| a.output_tokens)
        .filter(|t| *t > 0)
        .sum();
    metrics.tokens_per_minute = if wall > 0.0 {
        (output_tokens as f64 / wall * 60.0) as i64
    } else {
        0
    };
    metrics.startup_latency = if first.is_finite() {
        f32_of(first - input.started_at)
    } else {
        0.0
    };
    apply_performance(&mut metrics, &answers);
    apply_confidence(&mut metrics, &answers);
    metrics.input_tokens = answers.iter().map(|a| a.input_tokens).sum();
    metrics.output_tokens = answers.iter().map(|a| a.output_tokens).sum();
    metrics.total_tokens = metrics.input_tokens + metrics.output_tokens;
    metrics.total_cost_usd = answers
        .iter()
        .map(|a| f64::from(a.input_cost) + f64::from(a.output_cost))
        .sum();
    metrics.created_at_utc = Some(timestamp(input.started_at));
    metrics.completed_at_utc = Some(timestamp(if last.is_finite() {
        last
    } else {
        input.started_at
    }));
    metrics
}

#[cfg(test)]
mod tests {
    use serde_json::Value;

    use super::*;

    const FIXTURES: &str = include_str!("../../mpac_inference/testdata/inference_fixtures.json");

    fn fixtures() -> Value {
        serde_json::from_str(FIXTURES).unwrap()
    }

    fn strings(value: &Value) -> Vec<String> {
        serde_json::from_value(value.clone()).unwrap()
    }

    #[test]
    fn weighted_prf_matches_sklearn() {
        for case in fixtures()["metrics"].as_array().unwrap() {
            let scores = weighted_prf(&strings(&case["truth"]), &strings(&case["pred"]));
            for (name, actual) in [
                ("precision", scores.precision),
                ("recall", scores.recall),
                ("f1", scores.f1),
            ] {
                let expected = case[name].as_f64().unwrap();
                assert!(
                    (actual - expected).abs() < 1e-12,
                    "{name}: {actual} vs {expected} for {case}"
                );
            }
        }
    }

    #[test]
    fn percentiles_match_python_statistics() {
        for case in fixtures()["percentiles"].as_array().unwrap() {
            let values: Vec<f64> = serde_json::from_value(case["values"].clone()).unwrap();
            let expected: Vec<f64> = serde_json::from_value(case["percentiles"].clone()).unwrap();
            let (p50, p95, p99) = percentiles(&values);
            for (actual, expected) in [p50, p95, p99].iter().zip(&expected) {
                assert!(
                    (actual - expected).abs() < 1e-9,
                    "{actual} vs {expected} for {values:?}"
                );
            }
        }
    }

    #[test]
    fn grades_consensus_and_local_cost_match_python() {
        let fixtures = fixtures();
        for case in fixtures["grades"].as_array().unwrap() {
            let score = case["score"].as_f64().unwrap();
            assert_eq!(grade(score), case["grade"].as_str().unwrap(), "{score}");
            assert_eq!(
                consensus(score),
                case["confidence"].as_str().unwrap(),
                "{score}"
            );
        }
        for case in fixtures["local_costs"].as_array().unwrap() {
            let expected = case["cost"].as_f64().unwrap();
            assert!(
                (local_inference_cost(case["seconds"].as_f64().unwrap()) - expected).abs() < 1e-15
            );
        }
    }

    #[test]
    fn responder_metrics_for_an_evaluation() {
        let sample: Vec<TestItem> = (0..4)
            .map(|i| TestItem {
                id: format!("i{i}"),
                answer: if i % 2 == 0 {
                    "yes".into()
                } else {
                    "no".into()
                },
                is_relevant: i % 2 == 0,
                ..TestItem::default()
            })
            .collect();
        let answers: Vec<TimedAnswer> = sample
            .iter()
            .enumerate()
            .map(|(i, item)| TimedAnswer {
                answer: TestRunAnswer {
                    item_id: item.id.clone(),
                    answer: if i == 3 {
                        String::new()
                    } else {
                        item.answer.clone()
                    },
                    is_correct: i != 3,
                    attachment_type: FileModality::Text.into(),
                    status: 1,
                    task_duration: 1.0 + f32::from(u8::try_from(i).unwrap()),
                    ttft: 0.5,
                    output_tokens: 10,
                    input_tokens: 100,
                    input_cost: 0.01,
                    confidence: Some(0.5),
                    ..TestRunAnswer::default()
                },
                stored_at: 1000.0 + f64::from(u8::try_from(i).unwrap()),
            })
            .collect();
        let refs: Vec<&TimedAnswer> = answers.iter().collect();

        let metrics = responder_metrics(&ResponderInput {
            responder_id: "m1",
            model_id: "m1",
            evaluation: true,
            sample: &sample,
            answers: &refs,
            started_at: 999.0,
        });

        assert_eq!(
            (
                metrics.total,
                metrics.num_correct,
                metrics.num_refusals_errors,
                metrics.num_positives
            ),
            (4, 3, 1, 2)
        );
        assert_eq!(metrics.simple_grade, "⭐⭐⭐ Low Quality");
        assert_eq!(
            (
                metrics.true_positives,
                metrics.true_negatives,
                metrics.false_positives,
                metrics.false_negatives
            ),
            (2, 1, 1, 0)
        );
        assert!((metrics.refusal_error_rate - 0.25).abs() < 1e-12);
        assert_eq!(metrics.modality_scores.get("TEXT").copied(), Some(0.75));
        assert_eq!(metrics.tokens_per_minute, 600);
        assert!((metrics.startup_latency - 1.0).abs() < 1e-6);
        assert!(metrics.streamed);
        assert!((metrics.mean_confidence - 0.5).abs() < 1e-6);
        assert_eq!(metrics.total_tokens, 440);
    }
}
