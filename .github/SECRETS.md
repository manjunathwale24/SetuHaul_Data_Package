# GitHub Secrets Required for the Deploy Pipeline

The workflow `SetuHaul_Data_Package/.github/workflows/deploy.yml` requires the following
repository secrets. Add them in GitHub at:

**Settings → Secrets and variables → Actions → New repository secret**

| Secret | Description | Example |
|--------|-------------|---------|
| `AWS_ACCESS_KEY_ID` | AWS IAM access key with permissions for Lambda, ECR, EKS, and Secrets Manager | `AKIA...` |
| `AWS_SECRET_ACCESS_KEY` | AWS secret key for the above IAM user | `wJalr...` |
| `VERCEL_TOKEN` | Vercel API token for `vercel --prod` deployments | Create at vercel.com/account → tokens |

## IAM permissions needed

The IAM user/role whose keys are stored above needs at least:

- `lambda:UpdateFunctionCode`, `lambda:PublishVersion`, `lambda:InvokeFunction`
- `ecr:*` (create repository, get auth token, put image)
- `eks:DescribeCluster`, `eks:UpdateKubeconfig`
- `k8s` API RBAC: `patch`/`update` on deployments in the `setuhaul` namespace
- `cloudwatch:PutMetricData` (optional, used by the DevOps Agent locally)

## Vercel token

1. Go to https://vercel.com/account/tokens
2. Create a token (name it `setuhaul-deploy`)
3. Copy it and add as `VERCEL_TOKEN` in GitHub secrets

The Vercel project is already linked: `.vercel/project.json`
(`projectId: prj_GpntS6U8ySpQk6iEPtPwk70LtXi2`, org `team_0UdxAXStkxNUsSWTctKRgspR`).

## How the pipeline runs

On every push to `main` touching `devops_agent/**` or `SetuHaul_Data_Package/**`:

1. **test** — runs `e2e_tests.py` against the live EKS backend + Vercel UI
2. **deploy-lambda** — zips `devops_agent/lambda_function.py` and updates the `setuhaul-devops-agent` Lambda
3. **deploy-vercel** — runs `vercel --prod` from `SetuHaul_Data_Package/`
4. **deploy-eks** — builds/pushes Docker image to ECR, then applies k8s manifests to `setuhaul-cluster`
5. **verify** — invokes the Lambda, curls the Vercel URLs, and confirms the EKS rollout status

All deploy jobs depend on `test` passing; `verify` runs only after all three deploys succeed.
