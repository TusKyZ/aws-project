output "plan_role_arn" {
  description = "Set as GitHub repository variable AWS_PLAN_ROLE_ARN."
  value       = aws_iam_role.plan.arn
}

output "apply_role_arn" {
  description = "Set as GitHub repository variable AWS_APPLY_ROLE_ARN."
  value       = aws_iam_role.apply.arn
}

output "github_variable_commands" {
  description = "gh CLI commands that wire CI to these roles (workflows no-op until the variables exist)."
  value       = <<-EOT
    gh variable set AWS_PLAN_ROLE_ARN --body "${aws_iam_role.plan.arn}"
    gh variable set AWS_APPLY_ROLE_ARN --body "${aws_iam_role.apply.arn}"
  EOT
}
