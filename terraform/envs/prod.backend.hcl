# terraform init -backend-config=envs/prod.backend.hcl
# CHANGEME: your state bucket (see backend.tf for the one-time bootstrap).
bucket = "CHANGEME-sentinel-tfstate"
key    = "sentinel/prod/terraform.tfstate"
region = "us-east-1"
