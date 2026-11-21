# Third-party notices and provenance

Project-owned source, including the local media service, downloader and workflow templates, is published under [Apache-2.0](LICENSE) with the repository owner's authorization. External components retain their applicable terms.

| Component | Provenance | Terms |
|---|---|---|
| `model_gateway/moss_edit_deps/mps_runtime.py` | [OpenMOSS/MOSS-Image](https://github.com/OpenMOSS/MOSS-Image/blob/6be4453024c5acae9dcd0a191e26ee524e743310/mps_runtime.py), Git blob `795d9cd500ea0346c8c7a4635bc3a89afc4a1b17` | [Apache-2.0](model_gateway/moss_edit_deps/LICENSE.OpenMOSS.txt) |
| `model_gateway/moss_edit_deps/pipeline_moss_image.py` | [OpenMOSS-Team/MOSS-Image-SFT](https://huggingface.co/OpenMOSS-Team/MOSS-Image-SFT/blob/f972bd4b4b9e3852133cc5da954d87bc32cd8fb9/pipeline_moss_image.py), Git blob `60ceb4b6962a6b3d3c57e404b4d7d26fa4b49b07` | [Apache-2.0](model_gateway/moss_edit_deps/LICENSE.OpenMOSS.txt) |
| `model_gateway/moss_edit.py` | Local MPS adapter for the reference-prefix editing contract attributed to chinoll/ComfyUI-Moss-Image-Edit | Upstream attribution is preserved in the module. The ComfyUI plugin and LoRA weights are external. |
| Bootstrap 5.3.9 | Vendored browser CSS from `bootstrap@5.3.9` | [MIT](model_gateway/assets/bootstrap/LICENSE) |
| Tabulator 6.6.1 | Vendored browser assets from `tabulator-tables@6.6.1` | [MIT](model_gateway/assets/tabulator/LICENSE) |

Model weights, external inference binaries and Python dependencies are installed separately under their respective licenses. See [the model and engine catalog](README.md#模型与引擎接入).

[Extraction provenance](docs/EXTRACTION_PROVENANCE.json) and [source snapshot](model_gateway/SOURCE_SNAPSHOT.json) record the local code baseline.
