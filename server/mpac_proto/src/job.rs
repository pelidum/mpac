use crate::mpac::jobs::job::Kind;

impl Kind {
    #[must_use]
    pub fn name(&self) -> &'static str {
        match self {
            Kind::AnswerItem(_) => "answer_item",
            Kind::FinalizeRun(_) => "finalize_run",
            Kind::BenchmarkStep(_) => "benchmark_step",
        }
    }
}

#[cfg(test)]
mod tests {
    use prost::{Message, Name};

    use super::*;
    use crate::mpac::jobs::{AnswerItem, BenchmarkStep, FinalizeRun, Job};
    use crate::{DESCRIPTOR_POOL, Model};

    fn every_kind() -> Vec<Kind> {
        vec![
            Kind::AnswerItem(AnswerItem::default()),
            Kind::FinalizeRun(FinalizeRun::default()),
            Kind::BenchmarkStep(BenchmarkStep::default()),
        ]
    }

    #[test]
    fn kind_names_match_oneof_field_names() {
        let job = DESCRIPTOR_POOL
            .get_message_by_name(&Job::full_name())
            .unwrap();
        let oneof = job.oneofs().find(|o| o.name() == "kind").unwrap();
        let mut fields: Vec<String> = oneof.fields().map(|f| f.name().to_owned()).collect();
        let mut names: Vec<String> = every_kind().iter().map(|k| k.name().to_owned()).collect();
        fields.sort();
        names.sort();

        assert_eq!(names, fields);
    }

    #[test]
    fn answer_item_job_round_trips() {
        let job = Job {
            kind: Some(Kind::AnswerItem(AnswerItem {
                run_id: "run".into(),
                item_id: "item".into(),
                responder_id: "debug/debug_random_model_1#1".into(),
                model: Some(Model {
                    id: "debug/debug_random_model_1".into(),
                    ..Model::default()
                }),
                include_reasoning: true,
            })),
        };

        assert_eq!(Job::decode(job.encode_to_vec().as_slice()).unwrap(), job);
    }
}
