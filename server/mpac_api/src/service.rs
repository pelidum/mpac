use mpac_proto::mpac_server::Mpac;
use mpac_proto::{
    Backend, BatchCreateTestItemsReply, BatchCreateTestItemsRequest, BatchGetAttachmentsRequest,
    Benchmark, BenchmarkRequest, BenchmarkResponse, CreateAttachmentUploadReply,
    CreateAttachmentUploadRequest, DeleteRequest, FileAttachment, GetCreditsReply, GetRequest,
    ListRequest, LoginRequest, Model, RunProgress, SecretAuditEvent, StatusReply, Test, TestItem,
    TestRun, TestRunAnswer, TestRunConfusionMatrix, TestRunReply, TestRunRequest, User,
};
use tonic::{Request, Response, Status};

use crate::Api;
use crate::status::{ResponseStream, stream};

#[tonic::async_trait]
impl Mpac for Api {
    async fn get_model(&self, request: Request<GetRequest>) -> Result<Response<Model>, Status> {
        crate::models::get_model(self, request)
            .await
            .map(Response::new)
    }

    type ListModelsStream = ResponseStream<Model>;

    async fn list_models(
        &self,
        request: Request<ListRequest>,
    ) -> Result<Response<Self::ListModelsStream>, Status> {
        crate::models::list_models(self, request)
            .await
            .map(|items| Response::new(stream(items)))
    }

    async fn get_credits(
        &self,
        request: Request<GetRequest>,
    ) -> Result<Response<GetCreditsReply>, Status> {
        crate::models::get_credits(self, request)
            .await
            .map(Response::new)
    }

    async fn create_backend(
        &self,
        request: Request<Backend>,
    ) -> Result<Response<StatusReply>, Status> {
        crate::backends::create_backend(self, request)
            .await
            .map(Response::new)
    }

    async fn get_backend(&self, request: Request<GetRequest>) -> Result<Response<Backend>, Status> {
        crate::backends::get_backend(self, request)
            .await
            .map(Response::new)
    }

    async fn update_backend(
        &self,
        request: Request<Backend>,
    ) -> Result<Response<StatusReply>, Status> {
        crate::backends::update_backend(self, request)
            .await
            .map(Response::new)
    }

    async fn delete_backend(
        &self,
        request: Request<DeleteRequest>,
    ) -> Result<Response<StatusReply>, Status> {
        crate::backends::delete_backend(self, request)
            .await
            .map(Response::new)
    }

    type ListBackendsStream = ResponseStream<Backend>;

    async fn list_backends(
        &self,
        request: Request<ListRequest>,
    ) -> Result<Response<Self::ListBackendsStream>, Status> {
        crate::backends::list_backends(self, request)
            .await
            .map(|items| Response::new(stream(items)))
    }

    type ListSecretAuditStream = ResponseStream<SecretAuditEvent>;

    async fn list_secret_audit(
        &self,
        request: Request<ListRequest>,
    ) -> Result<Response<Self::ListSecretAuditStream>, Status> {
        crate::backends::list_secret_audit(self, request)
            .await
            .map(|items| Response::new(stream(items)))
    }

    async fn get_benchmark(
        &self,
        request: Request<GetRequest>,
    ) -> Result<Response<Benchmark>, Status> {
        crate::benchmarks::get_benchmark(self, request)
            .await
            .map(Response::new)
    }

    type CreateBenchmarkStream = ResponseStream<BenchmarkResponse>;

    async fn create_benchmark(
        &self,
        request: Request<BenchmarkRequest>,
    ) -> Result<Response<Self::CreateBenchmarkStream>, Status> {
        crate::create::create_benchmark(self, request)
            .await
            .map(|items| Response::new(stream(items)))
    }

    async fn update_benchmark(
        &self,
        request: Request<Benchmark>,
    ) -> Result<Response<StatusReply>, Status> {
        crate::benchmarks::update_benchmark(self, request)
            .await
            .map(Response::new)
    }

    async fn delete_benchmark(
        &self,
        request: Request<DeleteRequest>,
    ) -> Result<Response<StatusReply>, Status> {
        crate::benchmarks::delete_benchmark(self, request)
            .await
            .map(Response::new)
    }

    type ListBenchmarksStream = ResponseStream<Benchmark>;

    async fn list_benchmarks(
        &self,
        request: Request<ListRequest>,
    ) -> Result<Response<Self::ListBenchmarksStream>, Status> {
        crate::benchmarks::list_benchmarks(self, request)
            .await
            .map(|items| Response::new(stream(items)))
    }

    async fn get_attachment(
        &self,
        request: Request<GetRequest>,
    ) -> Result<Response<FileAttachment>, Status> {
        crate::attachments::get_attachment(self, request)
            .await
            .map(Response::new)
    }

    async fn create_attachment(
        &self,
        request: Request<FileAttachment>,
    ) -> Result<Response<StatusReply>, Status> {
        crate::attachments::create_attachment(self, request)
            .await
            .map(Response::new)
    }

    async fn delete_attachment(
        &self,
        request: Request<DeleteRequest>,
    ) -> Result<Response<StatusReply>, Status> {
        crate::attachments::delete_attachment(self, request)
            .await
            .map(Response::new)
    }

    type ListAttachmentsStream = ResponseStream<FileAttachment>;

    async fn list_attachments(
        &self,
        request: Request<ListRequest>,
    ) -> Result<Response<Self::ListAttachmentsStream>, Status> {
        crate::attachments::list_attachments(self, request)
            .await
            .map(|items| Response::new(stream(items)))
    }

    type BatchGetAttachmentsStream = ResponseStream<FileAttachment>;

    async fn batch_get_attachments(
        &self,
        request: Request<BatchGetAttachmentsRequest>,
    ) -> Result<Response<Self::BatchGetAttachmentsStream>, Status> {
        crate::attachments::batch_get_attachments(self, request)
            .await
            .map(|items| Response::new(stream(items)))
    }

    async fn create_attachment_upload(
        &self,
        request: Request<CreateAttachmentUploadRequest>,
    ) -> Result<Response<CreateAttachmentUploadReply>, Status> {
        crate::attachments::create_attachment_upload(self, request)
            .await
            .map(Response::new)
    }

    async fn finalize_attachment(
        &self,
        request: Request<GetRequest>,
    ) -> Result<Response<FileAttachment>, Status> {
        crate::attachments::finalize_attachment(self, request)
            .await
            .map(Response::new)
    }

    async fn get_test(&self, request: Request<GetRequest>) -> Result<Response<Test>, Status> {
        crate::test_defs::get_test(self, request)
            .await
            .map(Response::new)
    }

    async fn create_test(&self, request: Request<Test>) -> Result<Response<StatusReply>, Status> {
        crate::test_defs::create_test(self, request)
            .await
            .map(Response::new)
    }

    async fn update_test(&self, request: Request<Test>) -> Result<Response<StatusReply>, Status> {
        crate::test_defs::update_test(self, request)
            .await
            .map(Response::new)
    }

    async fn delete_test(
        &self,
        request: Request<DeleteRequest>,
    ) -> Result<Response<StatusReply>, Status> {
        crate::test_defs::delete_test(self, request)
            .await
            .map(Response::new)
    }

    type ListTestsStream = ResponseStream<Test>;

    async fn list_tests(
        &self,
        request: Request<ListRequest>,
    ) -> Result<Response<Self::ListTestsStream>, Status> {
        crate::test_defs::list_tests(self, request)
            .await
            .map(|items| Response::new(stream(items)))
    }

    async fn get_test_item(
        &self,
        request: Request<GetRequest>,
    ) -> Result<Response<TestItem>, Status> {
        crate::test_items::get_test_item(self, request)
            .await
            .map(Response::new)
    }

    async fn create_test_item(
        &self,
        request: Request<TestItem>,
    ) -> Result<Response<StatusReply>, Status> {
        crate::test_items::create_test_item(self, request)
            .await
            .map(Response::new)
    }

    async fn batch_create_test_items(
        &self,
        request: Request<BatchCreateTestItemsRequest>,
    ) -> Result<Response<BatchCreateTestItemsReply>, Status> {
        crate::test_items::batch_create_test_items(self, request)
            .await
            .map(Response::new)
    }

    async fn update_test_item(
        &self,
        request: Request<TestItem>,
    ) -> Result<Response<StatusReply>, Status> {
        crate::test_items::update_test_item(self, request)
            .await
            .map(Response::new)
    }

    async fn delete_test_item(
        &self,
        request: Request<DeleteRequest>,
    ) -> Result<Response<StatusReply>, Status> {
        crate::test_items::delete_test_item(self, request)
            .await
            .map(Response::new)
    }

    type ListTestItemsStream = ResponseStream<TestItem>;

    async fn list_test_items(
        &self,
        request: Request<ListRequest>,
    ) -> Result<Response<Self::ListTestItemsStream>, Status> {
        crate::test_items::list_test_items(self, request)
            .await
            .map(|items| Response::new(stream(items)))
    }

    async fn get_test_run(
        &self,
        request: Request<GetRequest>,
    ) -> Result<Response<TestRun>, Status> {
        crate::runs::get_test_run(self, request)
            .await
            .map(Response::new)
    }

    async fn get_run_progress(
        &self,
        request: Request<GetRequest>,
    ) -> Result<Response<RunProgress>, Status> {
        crate::runs::get_run_progress(self, request)
            .await
            .map(Response::new)
    }

    async fn get_test_run_confusion_matrix(
        &self,
        request: Request<GetRequest>,
    ) -> Result<Response<TestRunConfusionMatrix>, Status> {
        crate::runs::get_test_run_confusion_matrix(self, request)
            .await
            .map(Response::new)
    }

    type CreateTestRunStream = ResponseStream<TestRunReply>;

    async fn create_test_run(
        &self,
        request: Request<TestRunRequest>,
    ) -> Result<Response<Self::CreateTestRunStream>, Status> {
        crate::create::create_test_run(self, request)
            .await
            .map(|items| Response::new(stream(items)))
    }

    async fn update_test_run(
        &self,
        request: Request<TestRun>,
    ) -> Result<Response<StatusReply>, Status> {
        crate::runs::update_test_run(self, request)
            .await
            .map(Response::new)
    }

    async fn delete_test_run(
        &self,
        request: Request<DeleteRequest>,
    ) -> Result<Response<StatusReply>, Status> {
        crate::runs::delete_test_run(self, request)
            .await
            .map(Response::new)
    }

    type ListTestRunsStream = ResponseStream<TestRun>;

    async fn list_test_runs(
        &self,
        request: Request<ListRequest>,
    ) -> Result<Response<Self::ListTestRunsStream>, Status> {
        crate::runs::list_test_runs(self, request)
            .await
            .map(|items| Response::new(stream(items)))
    }

    async fn get_test_run_answer(
        &self,
        request: Request<GetRequest>,
    ) -> Result<Response<TestRunAnswer>, Status> {
        let _ = request;
        Err(Status::unimplemented("GetTestRunAnswer is not implemented"))
    }

    async fn create_test_run_answer(
        &self,
        request: Request<TestRunRequest>,
    ) -> Result<Response<StatusReply>, Status> {
        let _ = request;
        Err(Status::unimplemented(
            "CreateTestRunAnswer is not implemented",
        ))
    }

    async fn delete_test_run_answer(
        &self,
        request: Request<DeleteRequest>,
    ) -> Result<Response<StatusReply>, Status> {
        let _ = request;
        Err(Status::unimplemented(
            "DeleteTestRunAnswer is not implemented",
        ))
    }

    type ListTestRunAnswersStream = ResponseStream<TestRunAnswer>;

    async fn list_test_run_answers(
        &self,
        request: Request<ListRequest>,
    ) -> Result<Response<Self::ListTestRunAnswersStream>, Status> {
        crate::answers::list_test_run_answers(self, request)
            .await
            .map(|items| Response::new(stream(items)))
    }

    async fn get_user(&self, request: Request<GetRequest>) -> Result<Response<User>, Status> {
        crate::users::get_user(self, request)
            .await
            .map(Response::new)
    }

    async fn create_user(&self, request: Request<User>) -> Result<Response<StatusReply>, Status> {
        crate::users::create_user(self, request)
            .await
            .map(Response::new)
    }

    async fn update_user(&self, request: Request<User>) -> Result<Response<StatusReply>, Status> {
        crate::users::update_user(self, request)
            .await
            .map(Response::new)
    }

    async fn delete_user(
        &self,
        request: Request<DeleteRequest>,
    ) -> Result<Response<StatusReply>, Status> {
        crate::users::delete_user(self, request)
            .await
            .map(Response::new)
    }

    type ListUsersStream = ResponseStream<User>;

    async fn list_users(
        &self,
        request: Request<ListRequest>,
    ) -> Result<Response<Self::ListUsersStream>, Status> {
        crate::users::list_users(self, request)
            .await
            .map(|items| Response::new(stream(items)))
    }

    async fn login_user(&self, request: Request<LoginRequest>) -> Result<Response<User>, Status> {
        crate::users::login_user(self, request)
            .await
            .map(Response::new)
    }
}
