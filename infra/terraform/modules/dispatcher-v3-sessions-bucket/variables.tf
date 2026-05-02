variable "bucket_name" {
  description = "Name of the S3 bucket for dispatcher-v3 session logs. Must be globally unique. Production-style name lives in dev only because v3 has no production footprint."
  type        = string
  default     = "judgemind-dispatcher-v3-sessions"
}

variable "environment" {
  description = "Deployment environment. v3 is dev-only by spec section 10; the variable exists so future tooling can grep for environment scope but production is not a supported value."
  type        = string

  validation {
    condition     = contains(["dev"], var.environment)
    error_message = "environment must be 'dev'. Dispatcher v3 has no production footprint per spec section 10."
  }
}

variable "agent_task_role_arn" {
  description = "ARN of the dispatcher-v3 agent_task_role (F3, #3921). Wired into the bucket policy as the only principal allowed to PutObject and GetObject. Coming from module.dispatcher_v3_iam.agent_task_role_arn."
  type        = string

  validation {
    # Reject empty strings early. A blank ARN would render the bucket
    # policy as `Principal.AWS = ""` which IAM accepts but resolves to
    # 'no principal' so the entire Allow statement becomes a no-op,
    # leaving the bucket effectively read-only-from-public-access-block
    # and write-only via the implicit owner. Better to fail at plan time.
    condition     = length(var.agent_task_role_arn) > 0
    error_message = "agent_task_role_arn must be a non-empty IAM role ARN."
  }
}

variable "session_retention_days" {
  description = "Number of days to retain session log objects before expiration. Default 365 (1 year). Operators can extend (e.g. 730 for two-year audit horizon) without changing the module. Per spec section 12.1, raw stream-json transcripts can be 50-200 MB so the lifecycle rule moves them to Glacier-IR at 30 days regardless of this value."
  type        = number
  default     = 365

  validation {
    # Lower bound guards against an accidental 0 (which would expire
    # objects on creation day, before the diagnoser can even read
    # them). Upper bound is generous; operators who need a true
    # archive can layer their own retention rule outside this module.
    condition     = var.session_retention_days >= 31 && var.session_retention_days <= 3650
    error_message = "session_retention_days must be between 31 and 3650 (10 years). The lifecycle rule transitions to Glacier-IR at day 30 so retention must exceed that floor."
  }
}
