use mpac_proto::json::to_jsonb;
use mpac_proto::{Benchmark, BenchmarkMatch, BenchmarkMatchResponse, ResponseCode, TestRun};
use mpac_queue::ClaimedJob;
use prost::Message;
use rand::seq::{IndexedRandom, SliceRandom};
use sqlx::{PgConnection, Row};

use crate::runs::{NewRun, create_run_in, now};
use crate::{Context, HandlerError, HandlerResult, Outcome};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Slot {
    pub round: i32,
    pub match_index: i32,
    pub first: String,
    pub second: Option<String>,
    pub run_id: Option<String>,
}

pub fn total_rounds(models: usize) -> i64 {
    if models < 2 {
        return 0;
    }
    i64::from((models - 1).bit_width()).max(1)
}

async fn slots(conn: &mut PgConnection, benchmark_id: &str) -> Result<Vec<Slot>, sqlx::Error> {
    let rows = sqlx::query(
        "SELECT round, match_index, first_model, second_model, run_id FROM benchmark_runs
         WHERE benchmark_id = $1 ORDER BY round, match_index",
    )
    .bind(benchmark_id)
    .fetch_all(&mut *conn)
    .await?;
    rows.iter()
        .map(|row| {
            Ok(Slot {
                round: row.try_get("round")?,
                match_index: row.try_get("match_index")?,
                first: row.try_get("first_model")?,
                second: row.try_get("second_model")?,
                run_id: row.try_get("run_id")?,
            })
        })
        .collect()
}

async fn load_run(conn: &mut PgConnection, run_id: &str) -> Result<Option<TestRun>, sqlx::Error> {
    let bytes: Option<Vec<u8>> =
        sqlx::query_scalar("SELECT proto_bytes FROM test_runs WHERE id = $1")
            .bind(run_id)
            .fetch_optional(&mut *conn)
            .await?;
    Ok(bytes.and_then(|b| TestRun::decode(b.as_slice()).ok()))
}

fn assign_bye(entrants: &mut Vec<String>, earlier: &[Slot]) {
    if entrants.len().is_multiple_of(2) {
        return;
    }
    let byes = |model: &str| {
        earlier
            .iter()
            .filter(|slot| slot.second.is_none() && slot.first == model)
            .count()
    };
    if let Some(index) = (0..entrants.len())
        .rev()
        .min_by_key(|&index| byes(&entrants[index]))
    {
        let holder = entrants.remove(index);
        entrants.push(holder);
    }
}

async fn start_round(
    conn: &mut PgConnection,
    benchmark: &Benchmark,
    round: i32,
    entrants: &[String],
) -> Result<(), HandlerError> {
    for (index, pair) in entrants.chunks(2).enumerate() {
        let match_index = i32::try_from(index).unwrap_or(i32::MAX);
        let (second, run_id) = match pair {
            [first, second] => {
                let run = create_run_in(
                    conn,
                    NewRun {
                        test_id: benchmark.test_id.clone(),
                        owner: benchmark.owner.clone(),
                        models: benchmark
                            .models
                            .iter()
                            .filter(|m| m.id == *first || m.id == *second)
                            .cloned()
                            .collect(),
                        include_reasoning: benchmark.include_reasoning,
                        sample_size: benchmark.sample_size,
                        backend_id: String::new(),
                        benchmark_id: Some(benchmark.id.clone()),
                    },
                )
                .await?;
                (Some(second.clone()), Some(run.id))
            }
            _ => (None, None),
        };
        sqlx::query(
            "INSERT INTO benchmark_runs (benchmark_id, round, match_index, first_model, second_model, run_id)
             VALUES ($1, $2, $3, $4, $5, $6)",
        )
        .bind(&benchmark.id)
        .bind(round)
        .bind(match_index)
        .bind(&pair[0])
        .bind(second)
        .bind(run_id)
        .execute(&mut *conn)
        .await?;
    }
    Ok(())
}

fn response(responder: &str, run: Option<&TestRun>) -> (BenchmarkMatchResponse, f64) {
    let metrics = run.and_then(|r| r.metrics.iter().find(|m| m.responder_id == responder));
    #[allow(clippy::cast_possible_truncation)]
    let score = |value: Option<f64>| value.map_or(-1.0, |v| v as f32);
    let f1 = metrics.map_or(-1.0, |m| m.f1);
    (
        BenchmarkMatchResponse {
            responder: responder.to_owned(),
            run_id: run.map(|r| r.id.clone()).unwrap_or_default(),
            precision: score(metrics.map(|m| m.precision)),
            recall: score(metrics.map(|m| m.recall)),
            f1: score(metrics.map(|m| m.f1)),
        },
        if f1 >= 0.0 { f1 } else { -1.0 },
    )
}

fn is_recorded(benchmark: &Benchmark, slot: &Slot) -> bool {
    benchmark
        .match_history
        .iter()
        .any(|m| m.round == i64::from(slot.round) && m.id == i64::from(slot.match_index) + 1)
}

fn record_match(benchmark: &mut Benchmark, slot: &Slot, run: Option<&TestRun>) {
    let mut matched = BenchmarkMatch {
        round: i64::from(slot.round),
        id: i64::from(slot.match_index) + 1,
        ..BenchmarkMatch::default()
    };
    match &slot.second {
        Some(second) => {
            if let Some(run) = run {
                #[allow(clippy::cast_possible_truncation)]
                let total =
                    (f64::from(benchmark.total_cost_usd) + f64::from(run.total_cost_usd)) as f32;
                benchmark.total_cost_usd = total;
            }
            let (first_response, first_score) = response(&slot.first, run);
            let (second_response, second_score) = response(second, run);
            #[allow(clippy::float_cmp)]
            let winner = if first_score == second_score {
                [slot.first.clone(), second.clone()]
                    .choose(&mut rand::rng())
                    .cloned()
                    .unwrap_or_default()
            } else if first_score > second_score {
                slot.first.clone()
            } else {
                second.clone()
            };
            matched.responses = vec![first_response, second_response];
            matched.winner = winner;
        }
        None => {
            matched.responses = vec![BenchmarkMatchResponse {
                responder: slot.first.clone(),
                ..BenchmarkMatchResponse::default()
            }];
            matched.winner.clone_from(&slot.first);
        }
    }
    benchmark.match_history.push(matched);
}

async fn record_finished(
    conn: &mut PgConnection,
    benchmark: &mut Benchmark,
    round_slots: &[Slot],
) -> Result<(), HandlerError> {
    for slot in round_slots {
        if is_recorded(benchmark, slot) {
            continue;
        }
        let run = match &slot.run_id {
            Some(run_id) => load_run(conn, run_id).await?,
            None => None,
        };
        if run
            .as_ref()
            .is_some_and(|r| r.status == i32::from(ResponseCode::InProgress))
        {
            continue;
        }
        record_match(benchmark, slot, run.as_ref());
    }
    benchmark.match_history.sort_by_key(|m| (m.round, m.id));
    Ok(())
}

fn round_winners(benchmark: &Benchmark, round: i32) -> Vec<String> {
    benchmark
        .match_history
        .iter()
        .filter(|m| m.round == i64::from(round))
        .map(|m| m.winner.clone())
        .collect()
}

async fn open_round(
    conn: &mut PgConnection,
    benchmark: &mut Benchmark,
    round: i32,
    entrants: &[String],
) -> Result<(), HandlerError> {
    start_round(conn, benchmark, round, entrants).await?;
    let opened: Vec<Slot> = slots(conn, &benchmark.id)
        .await?
        .into_iter()
        .filter(|s| s.round == round)
        .collect();
    record_finished(conn, benchmark, &opened).await
}

fn finish(benchmark: &mut Benchmark, winner: Option<&String>) {
    benchmark.winner = winner.cloned().unwrap_or_default();
    benchmark.status = ResponseCode::Success.into();
    benchmark.completed_at_utc = Some(now());
}

async fn step(conn: &mut PgConnection, benchmark_id: &str) -> Result<(), HandlerError> {
    let bytes: Option<Vec<u8>> =
        sqlx::query_scalar("SELECT proto_bytes FROM benchmarks WHERE id = $1 FOR UPDATE")
            .bind(benchmark_id)
            .fetch_optional(&mut *conn)
            .await?;
    let Some(mut benchmark) = bytes.and_then(|b| Benchmark::decode(b.as_slice()).ok()) else {
        return Ok(());
    };
    if benchmark.status != i32::from(ResponseCode::InProgress) {
        return Ok(());
    }

    let all = slots(conn, benchmark_id).await?;
    let current = all.iter().map(|s| s.round).max().unwrap_or(0);
    if current == 0 {
        let mut entrants: Vec<String> = benchmark.models.iter().map(|m| m.id.clone()).collect();
        entrants.shuffle(&mut rand::rng());
        if entrants.len() < 2 {
            finish(&mut benchmark, entrants.first());
        } else {
            open_round(conn, &mut benchmark, 1, &entrants).await?;
        }
    } else {
        let round_slots: Vec<Slot> = all.iter().filter(|s| s.round == current).cloned().collect();
        record_finished(conn, &mut benchmark, &round_slots).await?;
        if round_slots.iter().all(|s| is_recorded(&benchmark, s)) {
            let mut winners = round_winners(&benchmark, current);
            if winners.len() < 2 {
                finish(&mut benchmark, winners.first());
            } else {
                assign_bye(&mut winners, &all);
                open_round(conn, &mut benchmark, current + 1, &winners).await?;
            }
        }
    }
    sqlx::query("UPDATE benchmarks SET proto_jsonb = $1::jsonb, proto_bytes = $2 WHERE id = $3")
        .bind(to_jsonb(&benchmark)?)
        .bind(benchmark.encode_to_vec())
        .bind(benchmark_id)
        .execute(&mut *conn)
        .await?;
    Ok(())
}

pub async fn handle(ctx: &Context, job: &ClaimedJob, benchmark_id: &str) -> HandlerResult {
    let Some(mut tx) = crate::fenced(ctx, job).await? else {
        return Ok(Outcome::LeaseLost);
    };
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))")
        .bind(benchmark_id)
        .execute(&mut *tx)
        .await?;
    step(&mut tx, benchmark_id).await?;
    tx.commit().await?;
    Ok(Outcome::Committed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn total_rounds_matches_python_ceil_log2() {
        assert_eq!(
            [0, 1, 2, 3, 4, 5, 8, 9].map(total_rounds),
            [0, 0, 1, 2, 2, 3, 3, 4]
        );
    }

    fn play_bracket(models: usize, pick_winner: &mut impl FnMut() -> bool) -> (Vec<Slot>, i64) {
        let mut entrants: Vec<String> = (0..models).map(|i| format!("m{i}")).collect();
        let mut history = Vec::new();
        let mut round = 0;
        while entrants.len() > 1 {
            round += 1;
            assign_bye(&mut entrants, &history);
            let mut winners = Vec::new();
            for (index, pair) in entrants.chunks(2).enumerate() {
                let second = pair.get(1).cloned();
                let winner = match &second {
                    Some(second) if pick_winner() => second.clone(),
                    _ => pair[0].clone(),
                };
                history.push(Slot {
                    round,
                    match_index: i32::try_from(index).unwrap(),
                    first: pair[0].clone(),
                    second,
                    run_id: None,
                });
                winners.push(winner);
            }
            entrants = winners;
        }
        (history, i64::from(round))
    }

    fn bye_holders(history: &[Slot]) -> Vec<&str> {
        history
            .iter()
            .filter(|slot| slot.second.is_none())
            .map(|slot| slot.first.as_str())
            .collect()
    }

    #[test]
    fn seventeen_models_get_four_different_byes() {
        let (history, rounds) = play_bracket(17, &mut || false);

        let mut holders = bye_holders(&history);
        assert_eq!(holders.len(), 4);
        holders.sort_unstable();
        holders.dedup();
        assert_eq!(holders.len(), 4);
        assert_eq!(rounds, total_rounds(17));
    }

    #[test]
    fn a_second_bye_only_goes_to_a_round_where_everyone_already_had_one() {
        for models in 2..=70 {
            for _ in 0..50 {
                let (history, rounds) = play_bracket(models, &mut rand::random::<bool>);
                assert_eq!(rounds, total_rounds(models));
                for round in 1..=i32::try_from(rounds).unwrap() {
                    let this_round: Vec<&Slot> =
                        history.iter().filter(|s| s.round == round).collect();
                    let Some(bye) = this_round.iter().find(|s| s.second.is_none()) else {
                        continue;
                    };
                    let earlier: Vec<Slot> = history
                        .iter()
                        .filter(|s| s.round < round)
                        .cloned()
                        .collect();
                    let earlier_byes = bye_holders(&earlier);
                    if earlier_byes.contains(&bye.first.as_str()) {
                        let all_had_one = this_round.iter().all(|slot| {
                            earlier_byes.contains(&slot.first.as_str())
                                && slot
                                    .second
                                    .as_deref()
                                    .is_none_or(|second| earlier_byes.contains(&second))
                        });
                        assert!(all_had_one, "{models} models: {history:?}");
                    }
                }
            }
        }
    }

    #[test]
    fn the_bye_stays_last_and_other_entrants_keep_their_order() {
        let earlier = [Slot {
            round: 1,
            match_index: 1,
            first: "e".into(),
            second: None,
            run_id: None,
        }];
        let mut entrants: Vec<String> = ["a", "b", "c", "d", "e"].map(String::from).to_vec();

        assign_bye(&mut entrants, &earlier);

        assert_eq!(entrants, ["a", "b", "c", "e", "d"]);
    }

    #[test]
    fn even_rounds_have_no_bye_to_assign() {
        let mut entrants: Vec<String> = ["a", "b"].map(String::from).to_vec();

        assign_bye(&mut entrants, &[]);

        assert_eq!(entrants, ["a", "b"]);
    }

    #[test]
    fn missing_metrics_score_minus_one() {
        let (response, score) = response("m1", None);

        assert!((response.f1 + 1.0).abs() < f32::EPSILON);
        assert!((score + 1.0).abs() < f64::EPSILON);
        assert_eq!(response.run_id, "");
    }
}
