import json
import os
import subprocess

# Requirements for script:
# 1. AWS CLI installed and configured with admin credentials
# 2. repo names: time4116/iac-smith (controller), time4116/iac-smith-demo-infra (target)

ROLE_NAME = "iac-smith-bedrock-role"
CONTROLLER_REPO = "time4116/iac-smith"
TARGET_REPO = "time4116/iac-smith-demo-infra"


def run(cmd):
    print(f"Running: {' '.join(cmd)}")
    return subprocess.check_output(cmd, text=True)


def main():
    print(f"--- Bootstrapping AWS Role for {CONTROLLER_REPO} ---")

    # 1. Get AWS Account ID
    account_id = json.loads(run(["aws", "sts", "get-caller-identity"]))["Account"]
    print(f"Account ID: {account_id}")

    # 2. Setup OIDC Provider if it doesn't exist
    print("Checking OIDC provider...")
    oidc_list = run(["aws", "iam", "list-open-id-connect-providers"])
    oidc_url = "token.actions.githubusercontent.com"

    if oidc_url not in oidc_list:
        print("Creating GitHub OIDC provider...")
        run(
            [
                "aws",
                "iam",
                "create-open-id-connect-provider",
                "--url",
                f"https://{oidc_url}",
                "--client-id-list",
                "sts.amazonaws.com",
                "--thumbprint-list",
                "6938fd4d98bab03faadb97b34396831e3780aea1",
            ]
        )

    # 3. Create Trust Policy
    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Federated": f"arn:aws:iam::{account_id}:oidc-provider/{oidc_url}"},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringLike": {f"{oidc_url}:sub": f"repo:{CONTROLLER_REPO}:*"},
                    "StringEquals": {f"{oidc_url}:aud": "sts.amazonaws.com"},
                },
            }
        ],
    }
    with open("trust-policy.json", "w") as f:
        json.dump(trust_policy, f)

    # 4. Create Role
    print(f"Creating/Updating role {ROLE_NAME}...")
    try:
        run(["aws", "iam", "get-role", "--role-name", ROLE_NAME])
        run(
            [
                "aws",
                "iam",
                "update-assume-role-policy",
                "--role-name",
                ROLE_NAME,
                "--policy-document",
                "file://trust-policy.json",
            ]
        )
    except subprocess.CalledProcessError:
        run(
            [
                "aws",
                "iam",
                "create-role",
                "--role-name",
                ROLE_NAME,
                "--assume-role-policy-document",
                "file://trust-policy.json",
            ]
        )

    # 5. Create Inline Policy for permissions
    # MVPS Permissions needed: Bedrock (Invoke), S3 (backend), DynamoDB (locking),
    # ECS/Fargate (target resources)
    permissions = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockAccess",
                "Effect": "Allow",
                "Action": "bedrock:InvokeModel",
                "Resource": "*",  # Scope to specific model ARN if preferred
            },
            {
                "Sid": "BackendStorage",
                "Effect": "Allow",
                "Action": [
                    "s3:CreateBucket",
                    "s3:GetBucketVersioning",
                    "s3:PutBucketVersioning",
                    "s3:GetBucketEncryption",
                    "s3:PutBucketEncryption",
                    "s3:GetBucketPublicAccessBlock",
                    "s3:PutBucketPublicAccessBlock",
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:ListBucket",
                ],
                "Resource": [
                    f"arn:aws:s3:::{TARGET_REPO.split('/')[-1]}*",
                    f"arn:aws:s3:::{TARGET_REPO.split('/')[-1]}*/*",
                ],
            },
            {
                "Sid": "BackendLocking",
                "Effect": "Allow",
                "Action": [
                    "dynamodb:CreateTable",
                    "dynamodb:DescribeTable",
                    "dynamodb:GetItem",
                    "dynamodb:PutItem",
                    "dynamodb:DeleteItem",
                ],
                "Resource": f"arn:aws:dynamodb:*:*:table/{TARGET_REPO.split('/')[-1]}*",
            },
            {
                "Sid": "ECSFargatePermissions",
                "Effect": "Allow",
                "Action": [
                    "ecs:CreateCluster",
                    "ecs:DescribeClusters",
                    "ecs:DeleteCluster",
                    "ec2:CreateVpc",
                    "ec2:DescribeVpcs",
                    "ec2:DeleteVpc",
                    "ec2:CreateSubnet",
                    "ec2:DescribeSubnets",
                    "ec2:DeleteSubnet",
                    "ec2:CreateSecurityGroup",
                    "ec2:DescribeSecurityGroups",
                    "ec2:DeleteSecurityGroup",
                ],
                "Resource": "*",
            },
        ],
    }
    with open("permissions.json", "w") as f:
        json.dump(permissions, f)

    print("Attaching permissions...")
    run(
        [
            "aws",
            "iam",
            "put-role-policy",
            "--role-name",
            ROLE_NAME,
            "--policy-name",
            "IaCSmithPermissions",
            "--policy-document",
            "file://permissions.json",
        ]
    )

    role_arn = f"arn:aws:iam::{account_id}:role/{ROLE_NAME}"
    print("\n--- SUCCESS ---")
    print(f"Role ARN: {role_arn}")
    msg = f"Use this for the AWS_BEDROCK_ROLE_ARN variable in your GitHub repo {CONTROLLER_REPO}."
    print(msg)

    # Cleanup
    os.remove("trust-policy.json")
    os.remove("permissions.json")


if __name__ == "__main__":
    main()
