terraform {
  required_version = ">= 1.11.0" # S3-native state locking (use_lockfile) is GA from 1.11

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.5"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.env
      ManagedBy   = "terraform"
      Repository  = "github.com/TusKyZ/aws-project"
    }
  }
}
