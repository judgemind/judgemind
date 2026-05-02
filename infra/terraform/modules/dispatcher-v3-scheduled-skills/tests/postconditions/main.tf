# Fixture for the dispatcher-v3-scheduled-skills postcondition tests.
#
# Validates two module-level `check` blocks fire correctly:
#   1. `task_def_arn_matches_family` -- a mismatched ARN raises a check
#      block error message at plan time.
#   2. `alert_topic_required_when_enabled` -- enabling alerts without
#      an SNS topic raises a check block error at plan time.
#
# Both are lightweight invariants that prevent the module from
# silently shipping a wedged config (the family-mismatch case would
# AccessDenied at every cron firing; the missing-topic case would
# leave the alarm with no destination).
#
# Postcondition assertions in modules with `check` blocks emit
# warnings rather than errors at plan time -- terraform's behavior is
# to issue a "Check block assertion failed" diagnostic and return
# warning-level non-fatal output, so the check.sh wrapper greps for
# the warning text rather than relying on a non-zero exit code.

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region                      = "us-west-2"
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true

  # The module uses data "aws_caller_identity" which would normally
  # call STS. The provider doesn't have a knob for stubbing that, so
  # the plan errors AFTER both check blocks fire. The check.sh wrapper
  # treats non-zero exit as acceptable when both warnings are visible
  # in the output -- which is what we exercise.
}

# Intentional mismatch: the ARN's family is `scheduled-skill-XYZ` but
# the variable says `scheduled-skill`. The check block must fire.
module "scheduled_skills_mismatch" {
  source = "../../"

  environment = "dev"

  ecs_cluster_arn                        = "arn:aws:ecs:us-west-2:111111111111:cluster/test-cluster"
  scheduled_skill_task_definition_arn    = "arn:aws:ecs:us-west-2:111111111111:task-definition/judgemind-dispatcher-v3-WRONG-FAMILY:1"
  scheduled_skill_task_definition_family = "judgemind-dispatcher-v3-scheduled-skill"
  task_subnet_ids                        = ["subnet-aaaaaaaaaaaaaaaaa"]
  task_security_group_id                 = "sg-bbbbbbbbbbbbbbbbb"

  execution_role_arn  = "arn:aws:iam::111111111111:role/exec"
  agent_task_role_arn = "arn:aws:iam::111111111111:role/agent"

  enable_alerts       = true
  alert_sns_topic_arn = "arn:aws:sns:us-west-2:111111111111:test-topic"
}

# Intentional missing-SNS-topic: enable_alerts=true but topic=""
module "scheduled_skills_missing_topic" {
  source = "../../"

  environment = "dev"

  ecs_cluster_arn                        = "arn:aws:ecs:us-west-2:111111111111:cluster/test-cluster"
  scheduled_skill_task_definition_arn    = "arn:aws:ecs:us-west-2:111111111111:task-definition/judgemind-dispatcher-v3-scheduled-skill:1"
  scheduled_skill_task_definition_family = "judgemind-dispatcher-v3-scheduled-skill"
  task_subnet_ids                        = ["subnet-aaaaaaaaaaaaaaaaa"]
  task_security_group_id                 = "sg-bbbbbbbbbbbbbbbbb"

  execution_role_arn  = "arn:aws:iam::111111111111:role/exec"
  agent_task_role_arn = "arn:aws:iam::111111111111:role/agent"

  enable_alerts       = true
  alert_sns_topic_arn = ""
}
