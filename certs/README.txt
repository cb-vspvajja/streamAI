Download the root CA certificate from your target Capella Operational cluster.
Save it in this folder with the exact filename: capella-ca.pem

This is the public database CA certificate, not a client private key.
The startup command mounts it read-only into Agent Memory and verifies TLS.
