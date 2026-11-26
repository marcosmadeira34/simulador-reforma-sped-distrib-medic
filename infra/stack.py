"""Stack de estudo: Calculadora RTC no Fargate + 3 Lambdas + Step Functions + SNS + S3.

Pensada para ser barata e descartável (`cdk destroy` apaga tudo):
- sem NAT Gateway e sem load balancer: a calculadora roda numa subnet pública, mas o security
  group só aceita conexões das Lambdas; as Lambdas a encontram por DNS privado (Cloud Map);
- as Lambdas ficam em subnets isoladas e falam com o S3 por um VPC endpoint gratuito.
"""

from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_chatbot as chatbot,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_stepfunctions as sfn,
)
from aws_cdk.aws_ecr_assets import Platform
from constructs import Construct

RAIZ = Path(__file__).resolve().parents[1]
INFRA = Path(__file__).resolve().parent
VERSAO_CALCULADORA = "1.6.1"  # igual ao docker-compose.yml e a [calculadora] versao_esperada


class SimuladorRtcStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        email = self.node.try_get_context("email_aprovacao")
        tarefas = int(self.node.try_get_context("calculadora_tarefas") or 1)

        # --- dados ------------------------------------------------------------------------
        bucket = s3.Bucket(
            self, "Dados",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # --- rede -------------------------------------------------------------------------
        vpc = ec2.Vpc(
            self, "Rede",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(name="publica", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24),
                ec2.SubnetConfiguration(name="isolada", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                                        cidr_mask=24),
            ],
            gateway_endpoints={"S3": ec2.GatewayVpcEndpointOptions(service=ec2.GatewayVpcEndpointAwsService.S3)},
        )
        sg_lambdas = ec2.SecurityGroup(self, "SgLambdas", vpc=vpc, description="Lambdas do simulador")
        sg_calculadora = ec2.SecurityGroup(self, "SgCalculadora", vpc=vpc, description="Calculadora RTC")
        sg_calculadora.add_ingress_rule(sg_lambdas, ec2.Port.tcp(8080), "Somente as Lambdas do simulador")

        # --- calculadora RTC (container Java) ----------------------------------------------
        cluster = ecs.Cluster(
            self, "Cluster", vpc=vpc,
            default_cloud_map_namespace=ecs.CloudMapNamespaceOptions(name="rtc.local", vpc=vpc),
        )
        tarefa = ecs.FargateTaskDefinition(
            self, "TarefaCalculadora", cpu=1024, memory_limit_mib=2048,
            runtime_platform=ecs.RuntimePlatform(cpu_architecture=ecs.CpuArchitecture.X86_64,
                                                 operating_system_family=ecs.OperatingSystemFamily.LINUX),
        )
        tarefa.add_container(
            "calculadora",
            image=ecs.ContainerImage.from_asset(
                str(RAIZ / "docker"), file="calculadora.Dockerfile",
                build_args={"RTC_REF": VERSAO_CALCULADORA}, platform=Platform.LINUX_AMD64,
            ),
            port_mappings=[ecs.PortMapping(container_port=8080)],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="calculadora",
                                           log_retention=logs.RetentionDays.ONE_WEEK),
        )
        ecs.FargateService(
            self, "Calculadora",
            cluster=cluster,
            task_definition=tarefa,
            desired_count=tarefas,
            min_healthy_percent=0,
            assign_public_ip=True,  # para baixar a imagem do ECR sem NAT Gateway
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            security_groups=[sg_calculadora],
            cloud_map_options=ecs.CloudMapOptions(name="calculadora", dns_ttl=Duration.seconds(10)),
        )

        # --- Lambdas: o mesmo pacote Python da CLI ---------------------------------------------
        codigo = lambda_.Code.from_asset(
            str(RAIZ),
            exclude=["venv", ".venv", ".git", "infra", "tests", "exemplos", "config", "docker",
                     "resultados*", "**/__pycache__", "**/*.egg-info", "build", "*Zone.Identifier"],
            bundling=BundlingOptions(
                image=lambda_.Runtime.PYTHON_3_13.bundling_image,
                command=["bash", "-c",
                         "cp -r /asset-input /tmp/src && pip install --no-cache-dir /tmp/src -t /asset-output"],
            ),
        )
        ambiente = {"BUCKET": bucket.bucket_name, "RTC_URL": "http://calculadora.rtc.local:8080/api"}

        def funcao(nome: str, handler: str, timeout: Duration) -> lambda_.Function:
            fn = lambda_.Function(
                self, nome,
                runtime=lambda_.Runtime.PYTHON_3_13,
                handler=f"simulador_rtc.aws.handlers.{handler}",
                code=codigo,
                memory_size=1024,
                timeout=timeout,
                environment=ambiente,
                vpc=vpc,
                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED),
                security_groups=[sg_lambdas],
                log_group=logs.LogGroup(self, f"Logs{nome}", retention=logs.RetentionDays.ONE_WEEK,
                                        removal_policy=RemovalPolicy.DESTROY),
            )
            bucket.grant_read_write(fn)
            return fn

        fn_conferir = funcao("Conferir", "conferir", Duration.minutes(2))
        fn_simular = funcao("SimularCenario", "simular_cenario", Duration.minutes(15))
        fn_consolidar = funcao("Consolidar", "consolidar", Duration.minutes(2))

        # --- aprovação humana ------------------------------------------------------------------
        topico = sns.Topic(self, "Aprovacao", display_name="Simulador RTC - aprovacao")
        if email:
            topico.add_subscription(subs.EmailSubscription(email))

        # --- alertas de falha no Slack -----------------------------------------------------
        # O estado NotificarFalha publica aqui; o AWS Chatbot assina o tópico e posta no canal.
        # Pré-requisito (uma vez, no console): AWS Chatbot > Configure new client > Slack,
        # autorizar o workspace. Depois: cdk deploy -c slack_workspace_id=T... -c slack_channel_id=C...
        alertas = sns.Topic(self, "Alertas", display_name="Simulador RTC - falhas")
        slack_workspace = self.node.try_get_context("slack_workspace_id")
        slack_canal = self.node.try_get_context("slack_channel_id")
        if slack_workspace and slack_canal:
            chatbot.SlackChannelConfiguration(
                self, "Slack",
                slack_channel_configuration_name="simulador-rtc-alertas",
                slack_workspace_id=slack_workspace,
                slack_channel_id=slack_canal,
                notification_topics=[alertas],
                logging_level=chatbot.LoggingLevel.ERROR,
            )

        # --- Step Functions ----------------------------------------------------------------
        # A definição fica em JSON (ASL) para ser lida e estudada; o CDK só troca os ${...}.
        maquina = sfn.StateMachine(
            self, "Simulacao",
            state_machine_name="simulador-rtc",
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_file(str(INFRA / "state_machine.asl.json")),
            definition_substitutions={
                "FnConferir": fn_conferir.function_arn,
                "FnSimular": fn_simular.function_arn,
                "FnConsolidar": fn_consolidar.function_arn,
                "TopicoAprovacao": topico.topic_arn,
                "TopicoAlertas": alertas.topic_arn,
            },
            timeout=Duration.days(8),
            tracing_enabled=True,
            logs=sfn.LogOptions(
                destination=logs.LogGroup(self, "LogsSimulacao", retention=logs.RetentionDays.ONE_WEEK,
                                          removal_policy=RemovalPolicy.DESTROY),
                level=sfn.LogLevel.ALL,
                include_execution_data=True,
            ),
        )
        # Com a definição vinda de arquivo o CDK não deduz as permissões: concedemos à mão.
        for fn in (fn_conferir, fn_simular, fn_consolidar):
            fn.grant_invoke(maquina)
        topico.grant_publish(maquina)
        alertas.grant_publish(maquina)

        CfnOutput(self, "Bucket", value=bucket.bucket_name)
        CfnOutput(self, "StateMachineArn", value=maquina.state_machine_arn)
        CfnOutput(self, "TopicoAprovacaoArn", value=topico.topic_arn)
        CfnOutput(self, "TopicoAlertasArn", value=alertas.topic_arn)
