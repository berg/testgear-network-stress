# What this repo's CI needs from AWS

A brief for whoever provisions it. It describes *what must be true*, not how to
declare it — Pulumi, Terraform, console, whatever the account already uses.

Everything here is read-only from CI's point of view. Nothing in this
repository ever writes to AWS.

---

## Why any of this exists

NI-VISA, R&S VISA, Keysight IO Libraries and TekVISA are all behind
click-throughs, and none of them may be redistributed. This repository is
**public** (`berg/testgear-network-stress`), so the installers cannot live in
it and cannot be baked into any image or cache CI produces. They live in a
private bucket, and a CI job fetches one, uses it inside the job, and discards
it.

Two properties matter more than anything else below:

1. **A fork pull request must never be able to assume the role.** The
   pyvisa-py workflow clones a caller-named repository at a caller-named ref
   and *executes it*. That is its purpose. Nothing that does that may hold a
   credential.
2. **The role must not be able to write.** A compromised CI job should not be
   able to replace a driver with a different one, which would silently change
   what every future run measures.

---

## 1. An S3 bucket

Suggested name `testgear-visa-vendor-<account-id>` — account-suffixed so it is
globally unique without being a useful guess. Region is your choice; whatever
it is, it must be given to CI as `AWS_REGION`.

| Setting | Required value | Why |
| --- | --- | --- |
| Block Public Access | all four flags on | Non-redistributable binaries. This is the one setting that is not a preference. |
| Object ownership | bucket-owner-enforced (ACLs disabled) | Nothing here needs ACLs, and a disabled feature cannot be misconfigured. |
| Versioning | enabled | So a bad upload is rolled back rather than re-downloaded from a click-through. |
| Default encryption | SSE-S3 (AES256) | KMS would need a second grant on the role and buys nothing: these are vendor installers, not secrets. If the account mandates KMS, the role needs `kms:Decrypt` on that key too. |
| Lifecycle | expire noncurrent versions after ~365 days | Keeps the bill flat. |
| Bucket policy | deny `s3:*` when `aws:SecureTransport` is false | Defence in depth. |
| Deletion | retain on stack/stack-equivalent teardown | The contents cannot be re-fetched automatically. Losing them costs an afternoon of accepting licence terms by hand. |

Cost is negligible: a few hundred MB stored, and roughly a gigabyte of egress a
month if the full run stays weekly.

### Object layout

```
manifest.json
drivers/ni/linux/ni-ubuntu2204-drivers-2025Q3.deb
drivers/rs/linux/rsvisa_7.2.3_amd64.deb
drivers/keysight/windows/IOLibSuite_2025_1.exe
drivers/tek/windows/TekVISA_411.exe
```

Only those two prefixes — `manifest.json` and `drivers/` — are readable by the
role, so anything else in the bucket is invisible to CI.

---

## 2. The manifest

`manifest.json` at the bucket root. **This is a data file, not infrastructure**
— it is edited with `aws s3 cp` and should not be managed by Pulumi, because
the whole point is that a driver version or a silent-install flag can be
corrected without a deploy.

`tools/fetch_vendor.py` reads it. Shape:

```json
{
  "version": 1,
  "artifacts": [
    {
      "backend": "ni",
      "os": "linux",
      "arch": "x86_64",
      "key": "drivers/ni/linux/ni-ubuntu2204-drivers-2025Q3.deb",
      "sha256": "4f0a...",
      "vendor_version": "NI-VISA 2025 Q3",
      "install": { "kind": "apt-repo-deb", "package": "ni-visa" }
    },
    {
      "backend": "keysight",
      "os": "windows",
      "arch": "x86_64",
      "key": "drivers/keysight/windows/IOLibSuite_2025_1.exe",
      "sha256": "1ab2...",
      "vendor_version": "Keysight IO Libraries 2025 Update 1",
      "install": {
        "kind": "exe",
        "args": ["UNVERIFIED"],
        "success_exit_codes": [0, 3010],
        "expect_reboot": true,
        "library": "C:\\Windows\\System32\\ktvisa32.dll",
        "services": ["Keysight IO Libraries Service"]
      }
    }
  ]
}
```

`sha256` is verified after download and a mismatch is fatal: a run that
measures a library it cannot identify is not a measurement. A backend with no
entry is not an error — that leg reports the implementation as unavailable and
the page draws the column with that reason.

The Windows `install.args` are **not yet known**. Keysight IO Libraries Suite
and TekVISA are InstallShield-family bundles; the flags need verifying by hand
on a throwaway Windows VM once. They live here rather than in code precisely so
that fixing them is an upload.

---

## 3. GitHub OIDC identity provider

Standard, and an account may only have one:

- URL `https://token.actions.githubusercontent.com`
- Audience (client id) `sts.amazonaws.com`
- Thumbprint `6938fd4d98bab03faadb97b34396831e3780aea1` if the API insists on
  one; AWS validates this provider against its own trust store now.

**If the account already has it, reuse it — do not create a second.**

---

## 4. The role

Name it whatever fits your conventions; `testgear-gha-vendor-read` is the one
the docs use. Max session duration 1 hour (CI asks for 15 minutes).

### Trust policy — this is the important part

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub":
          "repo:berg/testgear-network-stress:environment:vendor-drivers"
      }
    }
  }]
}
```

`StringEquals` throughout. **No wildcard anywhere in `sub`** — not
`repo:berg/*`, not `...:environment:*`, not a `StringLike`. If the provisioning
code makes that easy to get wrong, it is worth an assertion.

The subject only contains `environment:vendor-drivers` when the job declares
that GitHub Environment, and a fork pull request cannot enter one. That is why
the gate is the environment rather than a branch check: the branch rule reaches
AWS inside the token, so **AWS enforces it**, not YAML that a pull request
could edit.

Optionally tighter, if you want it pinned to specific workflow files — add to
the same `StringEquals`:

```json
"token.actions.githubusercontent.com:job_workflow_ref":
  "berg/testgear-network-stress/.github/workflows/_leg-linux.yml@refs/heads/main"
```

Only one value is allowed per condition key in `StringEquals`, and there are
two leg workflows (`_leg-linux.yml`, `_leg-windows.yml`), so this needs either
`ForAnyValue:StringEquals` with both, or two roles. Worth doing, but do it
after the simple version is working — and note it must be updated whenever a
workflow file is renamed, and temporarily widened to a feature branch during
bring-up.

### Permission policy

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadManifest",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::BUCKET/manifest.json"
    },
    {
      "Sid": "ReadDrivers",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:GetObjectVersion"],
      "Resource": "arn:aws:s3:::BUCKET/drivers/*"
    },
    {
      "Sid": "ListPrefixesOnly",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::BUCKET",
      "Condition": {
        "StringLike": { "s3:prefix": ["drivers/*", "manifest.json"] }
      }
    }
  ]
}
```

No `PutObject`, no `DeleteObject`, no `DeleteObjectVersion`, no bucket
configuration reads. Uploading a driver is a human with their own credentials.

---

## 5. What to hand back

Three values, which go into the GitHub Environment named `vendor-drivers` as
**variables** (not secrets — none of them is one, and OIDC is the actual
control):

| Variable | Value |
| --- | --- |
| `AWS_ROLE_ARN` | the role ARN |
| `AWS_REGION` | the bucket's region |
| `VENDOR_BUCKET` | the bucket name |

---

## 6. The GitHub side (not AWS, but it is half the gate)

Someone with repo admin must, once:

1. Create the environment **`vendor-drivers`**.
2. Set its deployment branches to **selected branches → `main` only**. This is
   what stops a fork PR, and what makes the OIDC `sub` above meaningful.
3. Add the three variables above to that environment.
4. Enable GitHub Pages with source **GitHub Actions**.

---

## 7. How to check it worked

In order, and the last two matter most:

```bash
# 1. From a workflow_dispatch on main, in a job that enters the environment:
aws sts get-caller-identity
aws s3 ls s3://$VENDOR_BUCKET/drivers/

# 2. It must be able to read the manifest and a driver:
aws s3 cp s3://$VENDOR_BUCKET/manifest.json -

# 3. It must NOT be able to write:
echo x | aws s3 cp - s3://$VENDOR_BUCKET/drivers/probe.txt   # expect AccessDenied

# 4. It must NOT work from a branch other than main.
# 5. It must NOT work from a fork pull request: open one and confirm no job
#    even attempts the assume-role.
```

A provisioning run that cannot demonstrate 3, 4 and 5 has not finished.
