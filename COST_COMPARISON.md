# MCP platform cost comparison

Planning model prepared 2026-10-26, USD, US East (N. Virginia), Linux/x86,
730 hours/month. These are calculated scenarios, not an AWS bill, a quote, or
measurements of 1,000 deployed servers. The local Floci demonstration has no AWS
infrastructure bill. Validate regional rates and quotas in AWS Pricing Calculator
before purchasing capacity.

## Published inputs and their sources

| Input | Published amount used | Qualification/source |
| --- | ---: | --- |
| Ordinary ECS orchestration | $0 | Underlying resources still billed; excludes ECS Managed Instances fees. [ECS pricing](https://aws.amazon.com/ecs/pricing/) |
| Ordinary EKS cluster, standard version support | $0.10/hour ($73/month) | Extended version support is $0.60/hour; neither Auto Mode nor provisioned control-plane tiers are included. [EKS pricing](https://aws.amazon.com/eks/pricing/) |
| Fargate CPU / memory | $0.04048/vCPU-hour / $0.004445/GB-hour | Published AWS example rates, not a fresh regional price-list API quote. [AWS FinOps comparison](https://aws.amazon.com/blogs/aws-cloud-financial-management/a-finops-guide-to-comparing-containers-and-serverless-functions-for-compute/), [Fargate pricing](https://aws.amazon.com/fargate/pricing/) |
| EC2 reference: m5.xlarge, 4 vCPU / 16 GiB | $0.192/hour ($140.16/month) | AWS-published reference example; revalidate current instance/region price before ordering. [AWS workload comparison](https://aws.amazon.com/blogs/industries/accelerating-fraud-detection-in-financial-services-with-rapids-accelerator-for-apache-spark-on-aws/) |
| ALB / capacity | $0.0225/hour + $0.008/LCU-hour | AWS US-East pricing example. LCUs depend on connections, bytes and rules. [ELB pricing](https://aws.amazon.com/elasticloadbalancing/pricing/) |
| NAT gateway / processed traffic | $0.045/hour + $0.045/GB | Per-AZ gateway footprint assumed below. [VPC pricing](https://aws.amazon.com/vpc/pricing/) |
| Public IPv4 | $0.005/address-hour | Address counts are assumptions. [VPC pricing](https://aws.amazon.com/vpc/pricing/) |
| gp3 storage | $0.08/GB-month | AWS pricing example; additional IOPS/throughput and snapshots excluded from this unit rate. [EBS pricing](https://aws.amazon.com/ebs/pricing/) |
| Private image storage | $0.10/GB-month | In-region ECR transfer has no ECR transfer charge; NAT processing can still apply. [ECR pricing](https://aws.amazon.com/ecr/pricing/) |
| Internet transfer | $0.09/charged GB | Example tier, before any free allowance; not valid for every volume/region. [AWS VPC transfer example](https://aws.amazon.com/vpc/pricing/) |

AWS examples establish published reference prices. They do not establish that
every rate is a newly verified account-specific price. Taxes, discounts, Spot,
Savings Plans, free tiers and credits are excluded.

## Consistent workload and sizing assumptions

A "server" means one compatible MCP project, with **two application replicas**
while active. Each application replica reserves **0.10 vCPU and 128 MiB**. This is
an illustrative lightweight workload, not a measured production requirement.
Tool execution, Redis throughput and long-lived sessions can require much more.
All options use an external shared Redis service; Redis capacity is not counted
as application capacity.

EKS options include a 25m CPU / 32 MiB queue proxy per application replica for
Knative. Thus each application pod requests 0.125 vCPU / 160 MiB. EKS+EC2 also
reserves 2 vCPU / 4 GiB per cluster for controllers, gateway, DNS and related
services. ECS reserves two gateway tasks totaling 0.5 vCPU / 1 GiB. These gateway
and platform budgets are assumptions, not load-tested sizing for 1,000 routes.

For both EC2 options, budget 3 usable vCPU / 12 GiB per reference node, with a
minimum of three nodes, plus a 30 GB gp3 boot disk per node. The remaining 25%
is a planning reserve. Three nodes are a cost floor for a future multi-AZ design,
not proof of failure capacity, placement or availability. No node autoscaler or
production AWS networking was installed for this evaluation.

Fargate rounds each ECS application/gateway task to 0.25 vCPU / 0.5 GB. The small
EKS application pod also fits that billable shape after its queue proxy and the
additional 256 MB EKS reservation. EKS platform services are budgeted as eight
always-running 0.25 vCPU / 1 GB billable pods. Larger requests can cross Fargate
rounding thresholds sharply. Requests/limits and Fargate profiles would need
adjustment before a real migration. [EKS Fargate pod sizing](https://docs.aws.amazon.com/eks/latest/userguide/fargate-pod-configuration.html)

| Scenario | Registered projects | Active application replicas | Assumed traffic/month |
| --- | ---: | ---: | --- |
| Small, continuously active | 10 | 20 | 100 GB charged internet egress; 100 GB NAT |
| Idle month | 10 | 0 | No application traffic; routing and Redis retained |
| 1,000 servers, continuously active | 1,000 | 2,000 | 10,000 GB charged internet egress; 10,000 GB NAT |

Idle assumes minimum application scale zero has been configured in each economic
alternative. ECS supports minimum capacity zero with a demand metric, but does
not provide the demonstrated Knative HTTP activator path in this project. Its
buffering/wake mechanism would require separate design and implementation.
[ECS service autoscaling](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/service-auto-scaling.html)

## Shared infrastructure allowance

All four alternatives receive the same allowance, so storage/network/Redis are
not silently omitted. Quantities and the unpriced budgets below are assumptions.

| Monthly item | Small | Idle | 1,000 servers |
| --- | ---: | ---: | ---: |
| One shared ALB: average LCUs 1 small / 0 idle / 20 large | $22.27 | $16.43 | $133.23 |
| NAT in three AZs | $98.55 | $98.55 | $98.55 |
| Six public IPv4 addresses: three ALB + three NAT | $21.90 | $21.90 | $21.90 |
| ECR: 1 GB retained images/project | $1.00 | $1.00 | $100.00 |
| Redis, persistence/backups allowance **(assumed budget, no verified SKU)** | $150.00 | $150.00 | $500.00 |
| DNS, secrets, logs/monitoring allowance **(assumed budget)** | $30.00 | $30.00 | $300.00 |
| Charged internet transfer | $9.00 | $0 | $900.00 |
| NAT processing | $4.50 | $0 | $450.00 |
| Inter-AZ transfer allowance **(assumed budget)** | $5.00 | $0 | $500.00 |
| **Common allowance, rounded after calculation** | **$342.22** | **$317.88** | **$3,003.68** |

The large-case ALB budget allows more than the average byte throughput of the
assumed 10 TB/month; connections, rule evaluations or peaks can cost more.
One ALB presumes a shared host-routing gateway rather than a target group/rule
per project. Gateway throughput, ALB LCUs, address requirements and AWS quotas
must be validated; they are not proven sufficient for 1,000 servers. ECR layers
may deduplicate; budgets assume no such savings. Redis memory, operations,
replication, AZ transfer and recovery objectives need independent sizing. These
allowances are not managed-Redis price quotes or guarantees.

## Calculated monthly comparison

Each cell is **compute + boot disks + cluster fee / total including common
allowance**. Rounded to cents at the end of each calculation.

| Option | Small | Idle | 1,000 servers |
| --- | ---: | ---: | ---: |
| ECS + Fargate | $198.22 / **$540.44** | $18.02 / **$335.90** | $18,038.07 / **$21,041.75** |
| ECS + EC2 | $427.68 / **$769.90** | $427.68 / **$745.56** | $9,551.52 / **$12,555.20** |
| EKS + EC2 | $500.68 / **$842.90** | $500.68 / **$818.56** | $12,048.04 / **$15,051.72** |
| EKS + Fargate | $338.26 / **$680.48** | $158.06 / **$475.93** | $18,178.11 / **$21,181.78** |

Reproduce with `P = 2 * active_projects`, `H = 730`, and
`F(cpu, gb) = H * (cpu * 0.04048 + gb * 0.004445)`:

```text
ECS EC2 nodes = max(3, ceil((P*0.10+0.5)/3), ceil((P*0.125+1)/12))
EKS EC2 nodes = max(3, ceil((P*0.125+2)/3), ceil((P*0.15625+4)/12))
node + disk = H*0.192 + 30*0.08 = $142.56/month
ECS Fargate = (P+2)*F(0.25,0.5)
ECS EC2 = ECS nodes*142.56
EKS EC2 = EKS nodes*142.56 + 73
EKS Fargate = P*F(0.25,0.5) + 8*F(0.25,1) + 73
total = platform subtotal + common scenario allowance
```

Small/large EC2 fleets are respectively 3/67 nodes for ECS and 3/84 for EKS.
The idle column assumes an already rightsized three-node fleet. **Scaling pods
to zero does not reduce the bill for existing EC2 nodes.** An existing 84-node
fleet left idle still costs its 84-node subtotal; consolidation/right-sizing is
separate future work. Requests, rollout surge, topology and scheduling fragments
can require more nodes than these arithmetic lower bounds. ECS task networking
(including ENI/trunking limits) and Kubernetes pod-IP limits must also permit
the assumed packing. No 1,000-server
capacity test was executed.

## Why EKS + EC2 is the selected direction

Small idle installations can be cheaper on Fargate. For many lightweight MCP
projects, EC2 can pack requests below Fargate's minimum billable task sizes and
share platform overhead. Under these particular assumptions both EC2 options
are cheaper at 1,000 continuously active projects. That advantage can disappear
with low utilization, larger requests, operational labor or different discounts.
ECS+EC2 is cheaper than EKS+EC2 in this model; Kubernetes is not inherently the
lowest-price orchestrator.

EKS+EC2 supplies the Kubernetes APIs used by the existing CLI and Knative's
request-based scaling, activation and revision routing. It also allows worker
instance selection and shared capacity management without Fargate pod-shape
rounding. The same application contract can later target self-hosted Kubernetes
with another registry, storage and ingress implementation.

Production availability would require workers across AZs, tested placement and
failure headroom, redundant ingress/Knative infrastructure, and redundant Redis
with backups and failover. This is a future design requirement. Two local MCP
replicas and one Docker-hosted k3s node do not verify production HA.

EKS+Fargate reduces host administration, but its pod constraints, per-pod
rounding and always-on platform footprint need validation for this architecture.
The current local Redis PVC cannot be copied directly: EKS Fargate cannot mount
EBS volumes. External managed Redis would be the comparison's dependency.
[EKS Fargate constraints](https://docs.aws.amazon.com/eks/latest/userguide/fargate.html)

Production estimates must add or validate security scanning/signing, WAF/TLS,
support, telemetry retention, backup restoration, deployment surge, tenant
isolation, control-plane/API quotas, Redis sizing and engineer operations time.
Multi-region costs and implementation are outside this evaluation.
