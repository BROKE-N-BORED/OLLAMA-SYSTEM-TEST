# The knowledge store

This folder is the retrieval store for OST's cognitive
scaffold. When the scaffold is on (or in the scaffold leg of
a pair), each question is matched against the documents
here and the closest passages are added to the prompt as
reference material.

- Add your own .txt or .md files: anything placed here is
  chunked (about 500 characters per chunk) and indexed the
  next time a scaffold run starts. No restart is needed
  between runs; the store is read fresh at run start.
- protocol.md (one folder up) is the protocol document the
  scaffold gives the model as its system prompt. Edit it
  freely — it is plain text.
- The learned/ subfolder is where OST writes its own notes
  after a scaffold run finishes: one document per model,
  listing the questions it faced, which it passed, and
  which chunks it used. Those notes join the store for the
  next run. Delete them if you want a clean store.
- In Pair mode the store is frozen for both legs: OST
  hashes the store before the pair starts and records that
  hash in both legs' results, and the learned notes are
  written only after the whole pair has finished.
