# Capella Data Processing workflow integration

Local and self-managed demo mode uses `tools/load_catalogue.py` to create JSON
catalogue documents and embeddings. To showcase the managed Capella Data
Processing Service:

1. Create a **Data from Capella** workflow in Capella AI Services.
2. Select the source collection that contains the catalogue documents.
3. Select a Model Service or OpenAI embedding model.
4. Map the workflow output vector field to `embedding` and retain the source
   text in `embeddingText`.
5. Let the workflow create its metadata collections, Eventing functions, and
   Vector Search index. Do not modify the generated `vectorization-meta-data`
   scope or workflow Eventing functions.
6. Set:

   ```dotenv
   DATA_PROCESSING_MODE=capella_workflow
   DATA_PROCESSING_WORKFLOW_ID=<workflow-id>
   ```

7. Use **Vector workflow status** in the StreamAI inspector. The application
   verifies the number of embedded catalogue documents and vector dimensions.

The application consumes workflow-produced vectors through the same Search and
Vector Search retrieval code used by the local loader.
