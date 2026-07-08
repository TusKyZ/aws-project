# Bootstrap root: GitHub OIDC federation for CI. Separate state from the main
# stack because these are account-level, once-per-account resources — the main
# stack can be destroyed and recreated without touching CI's ability to run.
#
#   terraform -chdir=terraform/bootstrap init -backend-config=backend.hcl
#   terraform -chdir=terraform/bootstrap apply

terraform {
  required_version = ">= 1.11.0"

  backend "s3" {
    # bucket/region come from backend.hcl (same CHANGEME bucket as the main
    # stack — see ../backend.tf for the one-time bucket bootstrap).
    key          = "sentinel/bootstrap/terraform.tfstate"
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project    = "sentinel"
      ManagedBy  = "terraform-bootstrap"
      Repository = "github.com/TusKyZ/aws-project"
    }
  }
}
