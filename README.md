# Paper Harvester & Archive

Central repository and storage archive for all research papers and PDFs used across the interactive research labs at [lab-simul](https://github.com/nomi181472/lab-simul).

## Structure

All research papers are systematically organized under the `papers/` directory by topic and corpus domain:

- `papers/`
  - `knowledge_engineering/`
  - `networking/`
  - `networking_oa/`
  - `networking_topcited/`
  - `neuroevolution/`
  - `neuroevolution_ERL/`
  - `neuroevolution_LLM_GenAI/`
  - `neuroevolution_NEAT/`
  - `neuroevolution_trading/`
  - `object_detection/`
  - `object_trackers/`
  - `pose_estimation/`
  - `transformers/`
  - `wifi_sensing/`

## Usage with Lab Web Application

The Next.js application and simulation engines reside in [nomi181472/lab-simul](https://github.com/nomi181472/lab-simul). To run the full application with all PDF previews locally, clone or link this `papers` directory into the `lab` project:

```bash
git clone https://github.com/nomi181472/paper-harvester.git
# Link or place the papers/ directory into your lab root:
# ln -s /path/to/paper-harvester/papers /path/to/lab/papers
```
