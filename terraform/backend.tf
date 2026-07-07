# Remote state: S3 backend with native locking (use_lockfile, GA since
# Terraform 1.11). No DynamoDB lock table — that pattern is deprecated.
#
# Bucket/key/region are supplied per environment at init time:
#   terraform init -backend-config=envs/dev.backend.hcl
#
# The state bucket itself is the one piece of infrastructure created outside
# Terraform (chicken-and-egg). One-time bootstrap:
#   aws s3api create-bucket --bucket <your-tfstate-bucket>
#   aws s3api put-bucket-versioning --bucket <your-tfstate-bucket> \
#     --versioning-configuration Status=Enabled
#   aws s3api put-public-access-block --bucket <your-tfstate-bucket> \
#     --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

terraform {
  backend "s3" {
    use_lockfile = true
  }
}
