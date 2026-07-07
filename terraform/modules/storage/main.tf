variable "name_prefix" {
  type = string
}

resource "aws_dynamodb_table" "audit" {
  name         = "${var.name_prefix}-audit"
  billing_mode = "PAY_PER_REQUEST" # zero idle cost between demos

  hash_key = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  # Audit records carry expires_at (epoch seconds); LATEST#<dataset> drift
  # baselines deliberately omit the attribute and never expire.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

output "table_name" {
  value = aws_dynamodb_table.audit.name
}

output "table_arn" {
  value = aws_dynamodb_table.audit.arn
}
