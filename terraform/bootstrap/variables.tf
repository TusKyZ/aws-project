variable "region" {
  description = "Region for the provider session (IAM resources are global)."
  type        = string
  default     = "us-east-1"
}

variable "github_repository" {
  description = "GitHub org/repo allowed to assume the CI roles."
  type        = string
  default     = "TusKyZ/aws-project"
}

variable "apply_environment" {
  description = "GitHub Actions environment whose required-reviewer approval gates the apply role."
  type        = string
  default     = "dev"
}
