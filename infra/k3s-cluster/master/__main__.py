import os
import pulumi
import pulumi_aws as aws
import pulumi_aws.ec2 as ec2

# Initialize the configuration object
config = pulumi.Config()

# Get the current organization and current stack dynamically
current_org = pulumi.get_organization()
current_stack = pulumi.get_stack()

# Get Config variables
master_instance_type = config.require('master-instance-type')
runner_instance_type = config.require('runner-instance-type')
ami = config.require('ami')
common_project_name = config.require('common-project-name')

# Construct the reference string to access exported variables from common project
common_ref_name = f"{current_org}/{common_project_name}/{current_stack}"

# Create the StackReference
common_ref = pulumi.StackReference(common_ref_name)

# Now pull outputs from common project
vpc_id = common_ref.get_output("vpc_id")
public_subnet_id = common_ref.get_output("public_subnet_id")
public_subnet_2_id = common_ref.get_output("public_subnet_2_id")
private_subnet_id = common_ref.get_output("private_subnet_id")
security_group_id = common_ref.get_output("security_group_id")
alb_security_group_id = common_ref.get_output("alb_security_group_id")
cluster_instance_profile_name = common_ref.get_output("cluster_instance_profile_name")
key_pair_key_name = common_ref.get_output("key_pair_key_name")

# EC2 instances
master_instance = ec2.Instance(
    'master-instance',
    instance_type=master_instance_type,
    ami=ami,
    subnet_id=private_subnet_id,
    vpc_security_group_ids=[security_group_id],
    key_name=key_pair_key_name,
    iam_instance_profile=cluster_instance_profile_name, # To access s3 bucket
    root_block_device=ec2.InstanceRootBlockDeviceArgs(
        volume_size=25,       # 25GB storage
        volume_type="gp3",    # gp3 is faster and cheaper than gp2
        delete_on_termination=True,
    ),
    tags={
        'Name': 'Master Node',
    }
)

git_runner_instance = ec2.Instance('git-runner-instance',
    instance_type=runner_instance_type,
    ami=ami,
    subnet_id=public_subnet_id,
    vpc_security_group_ids=[security_group_id],
    key_name=key_pair_key_name,
    tags={
        'Name': 'Git Runner',
    }
)

# Creating application load balancer
alb = aws.lb.LoadBalancer(
    'k3s-alb',
    internal=False,
    load_balancer_type="application",
    security_groups=[alb_security_group_id],
    subnets=[public_subnet_id, public_subnet_2_id],
    tags={"Name": "k3s-app-alb"},
)

# Create a target group from ALB to Nginx NodePort 30080
target_group = aws.lb.TargetGroup(
    "alb-k3s-tg",
    port=30080,           # NodePort of ingress-nginx or service
    protocol="HTTP",
    vpc_id=vpc_id,
    target_type="instance",
    health_check={
        "path": "/healthz",
        "protocol": "HTTP",
        "port": "traffic-port",
    },
    tags={"Name": "alb-k3s-tg"},
)

# ALB Listener
listener = aws.lb.Listener("http-alb-listener",
    load_balancer_arn=alb.arn,
    port=80,
    protocol="HTTP",
    default_actions=[aws.lb.ListenerDefaultActionArgs(
        type="forward",
        target_group_arn=target_group.arn,
    )],
)

# Output the instance IP addresses
pulumi.export('git_runner_public_ip', git_runner_instance.public_ip)
pulumi.export('master_private_ip', master_instance.private_ip)
pulumi.export("alb_dns", alb.dns_name)


pulumi.export('target_group_arn', target_group.arn)