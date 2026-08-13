# Third-party notices for the pilot bundle

This inventory is a technical aid, not legal advice. Before redistribution, generate an SBOM for the exact archive and have legal/security review the bundled binaries and their transitive codecs.

| Component | Pinned identity | Declared license | Use |
|---|---|---|---|
| whisper.cpp | v1.9.2, source commit `306c88f4d1286aec1bf96e544632897886af5501` | MIT | Local ASR runtime |
| Whisper large-v3 GGML weights | revision `362722b3fdcd2300b58a8286933ead1c48619667` | MIT as recorded in the model manifest | Local ASR weights |
| Silero VAD GGML | v5.1.2, HF revision `e5614ed76a5dd4b03fad5068c89efcd2617a9d1e` | MIT | Local voice activity detection |
| T-one | source commit `3c5b6c015038173840e62cea99e10cdb1c759116` | Apache-2.0 | CPU fallback ASR |
| huggingface-hub | 0.33.0 | Apache-2.0 | T-one staging only |
| NumPy | 1.26.4 | BSD-3-Clause | T-one audio/model arrays |
| ONNX Runtime | 1.22.0 | MIT | T-one local inference |
| pyctcdecode | 0.5.0 | Apache-2.0 | T-one decoder adapter |
| poetry-core | 2.1.1 | MIT | Pinned T-one build backend |
| miniaudio / pyminiaudio | 1.61 | MIT-0 / MIT | Local WAV, MP3, FLAC and OGG inspection/decoding |
| PyAV | 18.0.0 | BSD-3-Clause; wheels bundle FFmpeg libraries under their own licenses | Optional AAC decoding |
| uv | 0.11.13 | MIT OR Apache-2.0 | First-run staging only |
| actions/checkout | commit `3d3c42e5aac5ba805825da76410c181273ba90b1` (v7.0.1) | MIT | Model-free GitHub CI only |
| actions/setup-python | commit `5fda3b95a4ea91299a34e894583c3862153e4b97` (v7.0.0) | MIT | Model-free GitHub CI only |

Authoritative source URLs, revisions, file sizes and SHA-256 values for model artifacts are stored in `models/*.manifest.example.json` and the staging scripts. The table lists direct runtime/staging components; the exact delivered archive still requires an automatically generated SBOM and transitive-license inventory. Keep the copyright/license files shipped by each vendored binary distribution in the final archive.

Open FLEURS audio used for the local benchmark is licensed CC-BY-4.0 and is not stored in Git. Any evidence package that redistributes it must include the required attribution and its own file manifest.
