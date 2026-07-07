# terraform init -backend-config=envs/dev.backend.hcl
# CHANGEME: your state bucket (see backend.tf for the one-time bootstrap).
bucket = "CHANGEME-sentinel-tfstate"
key    = "sentinel/dev/terraform.tfstate"
region = "us-east-1"
