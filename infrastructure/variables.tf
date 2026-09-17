variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name"
  type        = string
  default     = "production"
}

variable "s3_bucket_name" {
  description = "S3 bucket name for avatar storage"
  type        = string
}

variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.t3.micro"
}

variable "db_username" {
  description = "Database username"
  type        = string
  default     = "avatar_user"
}

variable "db_password" {
  description = "Database password"
  type        = string
  sensitive   = true
}

variable "redis_node_type" {
  description = "ElastiCache node type"
  type        = string
  default     = "cache.t3.micro"
}

# ── ECS compute ──────────────────────────────────────────────────────────────

variable "project_name" {
  description = "Name prefix for all resources"
  type        = string
  default     = "avatar"
}

variable "gpu_instance_type" {
  description = <<-EOT
    EC2 instance backing the ECS cluster. MuseTalk needs a GPU, and Fargate
    has none — that is why this is an EC2-backed capacity provider rather
    than the Fargate setup the guide used to imply.
    g5.xlarge (A10G, 24 GB) hits ~30 FPS; g4dn.xlarge (T4, 16 GB) ~15-20.
  EOT
  type        = string
  default     = "g5.xlarge"
}

variable "gpu_instance_count" {
  description = <<-EOT
    Number of GPU instances. Each g5.xlarge has exactly ONE GPU, and an ECS
    task reserving a GPU takes the whole device — so the backend and a
    GPU-reserving worker cannot share one instance. Raise this before setting
    worker_uses_gpu.
  EOT
  type        = number
  default     = 1
}

variable "worker_uses_gpu" {
  description = <<-EOT
    Reserve a GPU for the Celery worker. Off by default: with one instance the
    backend already holds the only GPU, and a second GPU-reserving task would
    sit PENDING forever. With it off the worker still runs — its animation
    calls fall back to the `simple` engine, which is correct for cleanup and
    avatar-processing work.
  EOT
  type        = bool
  default     = false
}

variable "backend_image" {
  description = "Backend image URI. Defaults to the ECR repo this config creates."
  type        = string
  default     = ""
}

variable "frontend_image" {
  description = "Frontend image URI. Defaults to the ECR repo this config creates."
  type        = string
  default     = ""
}

variable "anthropic_api_key" {
  description = "Stored in SSM Parameter Store, never in the task definition."
  type        = string
  default     = ""
  sensitive   = true
}

variable "openai_api_key" {
  type      = string
  default   = ""
  sensitive = true
}

variable "jwt_secret_key" {
  description = "Must be >= 32 chars; Settings refuses to start otherwise."
  type        = string
  sensitive   = true
}

variable "secret_key" {
  description = "Must be >= 32 chars; Settings refuses to start otherwise."
  type        = string
  sensitive   = true
}

variable "cors_origins" {
  description = "Production requires https origins; the app refuses http in production."
  type        = string
  default     = ""
}
