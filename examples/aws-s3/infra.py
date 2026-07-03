"""AWS S3 bucket with tags and versioning.

A minimal example showing how to create cloud resources with tlumi.
Uses variables from tlumi.yaml for configuration. The `region` variable
is wired to an explicit AWS provider, so `--var region=...` controls
where the bucket is created.
"""

import pulumi
import pulumi_aws as aws
from pulumi_aws import s3

config = pulumi.Config()
environment = config.get("environment") or "dev"
region = config.get("region") or "us-east-1"

# Provider settings like the AWS region cannot be set through tlumi
# variables (variable keys cannot contain ":", so "aws:region" is not
# expressible). An explicit provider wires the variable in instead.
provider = aws.Provider("aws", region=region)

bucket = s3.BucketV2(
    "my-bucket",
    tags={
        "Environment": environment,
        "ManagedBy": "tlumi",
    },
    opts=pulumi.ResourceOptions(provider=provider),
)

versioning = s3.BucketVersioningV2(
    "my-bucket-versioning",
    bucket=bucket.id,
    versioning_configuration=s3.BucketVersioningV2VersioningConfigurationArgs(
        status="Enabled",
    ),
    opts=pulumi.ResourceOptions(provider=provider),
)

pulumi.export("bucket_name", bucket.bucket)
pulumi.export("bucket_arn", bucket.arn)
