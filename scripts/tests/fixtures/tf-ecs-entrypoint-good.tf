# tf-ecs-entrypoint-good.tf -- The #4270 fix shape.
#
# An aws_ecs_task_definition with an explicit `entryPoint` override on
# the container, so the runtime command is unambiguous regardless of the
# image's Dockerfile ENTRYPOINT. Mirror of the fix landed in #4260 for
# the field-population-audit module.

resource "aws_ecs_task_definition" "good_zero_record" {
  family                   = "judgemind-good-zero-record"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512

  container_definitions = jsonencode([
    {
      name       = "good-zero-record"
      image      = "fake-ecr/scraper:latest"
      entryPoint = ["python3"]
      command    = ["scripts/check-scraper-zero-record-runner.py"]
      essential  = true
    }
  ])
}
