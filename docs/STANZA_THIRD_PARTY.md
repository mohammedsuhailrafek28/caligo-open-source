# Third-party attribution: optional log NLP

Caligo's extractor, bridge, labels and tests are original. No Stanford source code,
training data or model weights are copied into this repository. Local use installs
Stanza and downloads only the versioned models listed in model-manifest.json.

- Stanford Stanza 1.15.0: Copyright 2019 The Board of Trustees of The Leland Stanford
  Junior University. [Apache License 2.0](https://github.com/stanfordnlp/stanza/blob/v1.15.0/LICENSE).
- The official [English model card](https://huggingface.co/stanfordnlp/stanza-en)
  declares Apache-2.0 for distributed English weights. Training resources have their
  own terms: [UD English EWT](https://github.com/UniversalDependencies/UD_English-EWT)
  identifies its dataset license as CC BY-SA 4.0. No EWT corpus is redistributed here.
- English lemma dictionaries may incorporate the SPECIALIST NLP Tools, release
  2026, from the US National Library of Medicine, a United States Government product.
  Review the [upstream attribution, terms and conversion modifications](https://stanfordnlp.github.io/stanza/combined_models.html#specialist-lexicon)
  before redistributing any model/dictionary. Caligo makes no modifications to those
  resources and claims no government endorsement or proprietary rights to them.
  This repository does not redistribute those resources. Preserve upstream notices
  and applicable conditions in any future binary/model distribution.
- PyTorch CPU dependencies are installed separately from the official
  [CPU wheel index](https://download.pytorch.org/whl/cpu); their installed notices apply.

Research attribution: Peng Qi, Yuhao Zhang, Yuhui Zhang, Jason Bolton and Christopher
D. Manning (2020), *Stanza: A Python Natural Language Processing Toolkit for Many
Human Languages*, ACL System Demonstrations.
[Paper](https://nlp.stanford.edu/pubs/qi2020stanza.pdf).

Processor selection follows the official [Stanza model documentation](https://stanfordnlp.github.io/stanza/models.html).
CPU/offline configuration is grounded in the installed 1.15.0 pipeline API and
its DownloadMethod.NONE implementation. No general RAM requirement is inferred
from unrelated full-pipeline benchmark figures; Caligo reports its measured usage.
