# Optional Agent Memory SDK wheel

The Docker build normally installs `couchbase-agent-memory` from the package index.
If Couchbase supplied the SDK as a wheel, place the `.whl` file in this directory
before running `./scripts/07-start-ui.sh`. The Dockerfile automatically prefers a
local wheel when one is present.
