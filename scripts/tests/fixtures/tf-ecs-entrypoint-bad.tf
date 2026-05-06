# tf-ecs-entrypoint-bad.tf -- Fixture replicating the #4270 shape.
#
# An aws_ecs_task_definition whose container `command` begins with an
# interpreter (python3) and does NOT declare an `entryPoint` override.
# When the image's Dockerfile ENTRYPOINT is ["python", "-m"] (the scraper
# image's pattern), the runtime command becomes `python -m python3 ...`
# which fails with "No module named python3" -- silent task failure.

resource "aws_ecs_task_definition" "bad_zero_record" {
  family                   = "judgemind-bad-zero-record"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512

  container_definitions = jsonencode([
    {
      name      = "bad-zero-record"
      image     = "fake-ecr/scraper:latest"
      command   = ["python3", "scripts/check-scraper-zero-record-runner.py"]
      essential = true
    }
  ])
}
