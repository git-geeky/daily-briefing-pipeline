# Technologies to Watch

Reference file for the daily intelligence briefing. When writing Part II (AI News) and Part III (Non-AI Tech) articles, subagents should check whether any story relates to technologies on this list. If a development advances, undermines, or recontextualizes a watchlist item, note it in the article. This list also informs Part I geopolitics coverage when technologies have defense or trade-policy implications.

Entries are organized by category with 1-2 sentence neutral descriptions. Last updated: 2026-04-16.

**Update cadence:** Reviewed alongside the monthly SOURCES.md audit. Technologies that have been fully deployed and mainstreamed should be graduated off the list. Technologies that have been abandoned or disproven should be removed with a note.

---

## Model Architecture Innovations

### LoopedLM (Ouro)
A parameter-shared looped transformer architecture where the same blocks are applied recurrently, enabling iterative computation in latent space. Ouro 1.4B and 2.6B models match the performance of models 3-4x their parameter count by leveraging superior knowledge manipulation rather than increased knowledge storage, with the architecture accepted for publication in 2025.

### Latent-space Path Diffusion
A family of techniques that perform diffusion-based generation in compressed latent representations rather than raw data space, reducing computational cost while maintaining output quality. Active research threads include LaDiR (Latent Diffusion Reasoner) for text reasoning via denoising thought tokens, and PDE-SSM approaches that replace attention in diffusion transformers with learnable convection-diffusion-reaction PDEs.

### Gated DeltaNet
A recurrent architecture from NVIDIA Labs (ICLR 2025) that combines Mamba2's gating mechanism with DeltaNet's delta update rule for complementary memory control -- gating enables rapid erasure while the delta rule facilitates targeted updates. Powers the Qwen3.5 and Qwen3-Next production models, with optimized FLA kernels reaching 45 Kt/s on H100 GPUs at the 1.3B scale.

### Mamba-3 SSMs
The third generation of the Mamba state space model family (ICLR 2026), introducing three improvements: a more expressive recurrence derived from SSM discretization, complex-valued state updates for richer state tracking, and a multi-input multi-output (MIMO) formulation. Achieves comparable perplexity to Mamba-2 with half the state size, and the MIMO variant adds 1.8 points of downstream accuracy over Gated DeltaNet at the 1.5B scale.

### Diffusion SSMs
Approaches that integrate state space models into diffusion-based generative pipelines to replace or augment attention. PDE-SSM-DiT matches or exceeds standard Diffusion Transformers at lower compute by modeling information flow via physically grounded dynamics, while temporal SSM layers enable video generation for 400+ frame sequences with 15-25% less memory than attention.

### CompreSSM
An in-training compression technique for state space models (ICLR 2026) that applies balanced truncation from control theory to identify and remove low-influence states based on Hankel singular values during training. SSMs that begin large and shrink during training retain task-critical structure that models trained directly at smaller dimension lose, while running more than 40x faster than spectral alternatives.

### Comba
A bilinear RNN variant that adopts scalar-plus-low-rank state transitions with both state feedback and output feedback corrections, grounded in closed-loop control theory. Demonstrates recall ability close to Transformers on MQAR and competitive performance on LongBench, with hardware-efficient Triton kernels added to the Flash Linear Attention library in June 2025.

### MIMO State Tracking
The multi-input multi-output formulation introduced in Mamba-3 that transitions recurrent models from SISO to MIMO structure, increasing the rank of input/output projections and transforming state updates from outer products to matrix-matrix multiplications. Increases decoding FLOPs by up to 4x at fixed state size but maintains similar wall-clock latency by overlaying computation with existing memory I/O; separately, IBM's NeurIPS 2025 spotlight paper presents structured sparse transition matrices for state tracking in SSMs.

### RWKV-7 "Goose"
A sequence modeling architecture with constant memory usage and constant inference time per token, introducing a generalized delta rule with vector-valued gating and in-context learning rates. The 2.9B parameter model achieves new 3B-class state-of-the-art on multilingual tasks despite training on fewer tokens than comparable models; RWKV-8 is in experimental stage as of late 2025.

### Mixture of Experts (MoE) Routing
The dominant scaling paradigm for frontier models, where only a fraction of total parameters activate per token. DeepSeek-V3 (671B total / 37B active) and successors demonstrate finely segmented expert routing with shared experts for common knowledge; from mid-2025, the focus shifted from increasing parameter count to making routing more efficient.

### Ring Attention
A distributed attention method that splits queries, keys, and values across devices in a ring topology, circulating KV blocks while overlapping communication with computation. Enables context lengths proportional to device count (tens of millions of tokens) without approximation or additional overhead; serves as the foundation for practical million-token context training.

### Diffusion Language Models (MDLM / LLaDA / Gemini Diffusion)
Text generation via iterative denoising rather than autoregressive token prediction. Google's Gemini Diffusion generates at 1,479 tokens/second (5x faster than comparable autoregressive models); MDLM powers ByteDance's Seed Diffusion; LLaDA matches LLaMA 3 performance training from scratch; DiDi-Instruct enables up to 64x acceleration through distillation.

### Native Multimodal / Omni Models
End-to-end architectures that process text, image, audio, and video within a single computational pipeline rather than stitching separate encoders onto a text backbone. Qwen3.5-Omni uses a native Audio Transformer pre-trained on 100M+ hours of audio-visual data; the field has shifted from experimental wrappers to unified perception and reasoning across modalities.

### BitNet / Ternary LLMs
Natively trained 1.58-bit large language models using weights restricted to {-1, 0, +1}. Microsoft released BitNet b1.58 2B4T in April 2025 (2B params, 4T training tokens, MIT license) with performance competitive to full-precision models; bitnet.cpp runs a 100B model on a single CPU at human reading speed with 71-82% energy reduction.

### Linear Attention Variants (GLA, Based, Lightning)
A family of methods replacing softmax attention with linear-complexity alternatives, trading some expressiveness for O(n) scaling. GLA (Gated Linear Attention) underpins several production models; Based introduces linear attention with Taylor expansion; Lightning Attention implements IO-aware tiling for linear attention kernels to achieve practical speedups.

### Hyena / Long Convolution Models
Sub-quadratic sequence models using implicit long convolutions parameterized by neural networks instead of attention. Hyena Hierarchy and successors like StripedHyena offer O(n log n) scaling with competitive quality at long context lengths; relevant as a non-SSM, non-attention alternative for sequence modeling.

---

## Training Techniques

### Muon, SOAP, Sophia, Lion, Kron & Scion Optimizers
A group of post-AdamW optimizers exploring different approaches to faster convergence: **Muon** orthogonalizes momentum via Newton-Schulz (up to 2x efficiency over AdamW); **SOAP** stabilizes Shampoo with Adam's second moment (ICLR 2025); **Sophia** approximates Hessian information at low cost; **Lion** uses coordinate-wise signed updates for memory savings; **Kron** applies Kronecker-factored PSGD preconditioning; **Scion** exploits neural network geometry with norm-constrained LMOs (ICML 2025). Muon, SOAP, and Kron show 30-40% single-step training speed improvements over tuned AdamW.

### Muon-Scale Optimizer Integration
The practical challenge of integrating non-AdamW optimizers into large-scale training infrastructure. NorMuon (2025) addresses scaling Muon efficiently; research shows Muon performs best under standard Chinchilla data ratios while SOAP excels when data-to-model ratios exceed 8x, suggesting optimizer selection should be workload-dependent rather than universal.

### Test-Time Compute Scaling
The paradigm of spending more compute at inference rather than training, letting models "think longer" through extended chain-of-thought. A 7B model with 100x inference compute can match a 70B model with standard inference; inference demand is projected to exceed training demand by 118x by 2026, reshaping GPU procurement toward inference-optimized hardware.

### GRPO / DAPO / Verifier-Driven RL
Post-training alignment methods that have largely replaced traditional RLHF with PPO. GRPO (Group Relative Policy Optimization), introduced by DeepSeek for R1, eliminates the critic network by scoring groups of 8-64 responses against verifiable rewards; DAPO further refines the approach; the 2026 production stack is modular: SFT + preference optimization (DPO/SimPO/KTO) + RL with verifiable rewards.

### Synthetic Data Generation
Using LLMs to produce artificial training examples via prompt-based generation, retrieval-augmented pipelines, and iterative self-refinement. Benefits include cost-effectiveness and controllable diversity for low-resource tasks; risks include factual inaccuracies, bias amplification, and model collapse when training recursively on synthetic outputs without human data anchoring.

### Knowledge Distillation & Structured Pruning
Compressing large models by transferring knowledge to smaller student models and removing unnecessary parameters. The optimal sequence identified in 2025 is Pruning then Distillation then Quantization (P-KD-Q); NVIDIA demonstrated 2-4x compression of Nemotron-4 family models with minimal quality loss, reducing deployment costs for edge and production inference.

### Curriculum Learning & Data Mixing
Strategies for ordering and proportioning training data to improve model quality without adding more data. Research in 2025-2026 focuses on optimal sequencing of difficulty levels, domain mixing ratios, and quality filtering, with findings that training data composition choices can be as impactful as architecture changes.

---

## Inference Optimization

### Quantized Johnson-Lindenstrauss (QJL)
A KV cache compression method that applies a Johnson-Lindenstrauss transform as a preconditioner to embedding vectors, then quantizes the result to a single sign bit (+1 or -1). Preserves geometric distances during aggressive compression with near-zero overhead, presented at AAAI 2025 and integrated into Google's TurboQuant framework.

### PolarQuant
A post-training weight quantization method (AISTATS 2026) that normalizes weights to the unit hypersphere, applies Walsh-Hadamard rotation to produce approximately Gaussian coordinates, then quantizes with distribution-matched centroids. Hadamard rotation alone accounts for 98% of quality improvement, reducing Qwen3.5-9B perplexity to within +0.03 of FP16 at 5-bit precision without any calibration data.

### TurboQuant
A training-free, data-oblivious vector quantization algorithm from Google Research (ICLR 2026) that compresses KV cache to 3 bits per value, achieving 6x memory reduction and up to 8x speedup on H100 GPUs without measurable accuracy loss. Operates online during inference, processing each vector as it arrives without calibration data; combines QJL and PolarQuant as components; open-source community implementations with vLLM integration are already available.

### FlashAttention-3 / FlashAttention-4
Attention kernel optimizations exploiting GPU hardware features for faster, memory-efficient exact attention. FA3 (Hopper GPUs) uses asynchronous Tensor Core/TMA interleaving and FP8 block quantization to reach 840 TFLOPS (BF16) / 1.3 PFLOPS (FP8) on H100; FA4 extends this with advanced kernel fusion for further IO minimization.

### Speculative Decoding (EAGLE-3 / Medusa)
Inference acceleration that predicts and verifies multiple tokens simultaneously using lightweight draft models. EAGLE-3 fuses hidden states from multiple intermediate layers of the target model, achieving 2-6x speedups (higher for 70B+ models); now built into vLLM, SGLang, and TensorRT-LLM as a production standard.

### PagedAttention / vAttention
Memory management techniques for KV cache inspired by OS virtual memory paging. PagedAttention (used in vLLM) achieves near-zero waste and flexible sharing within and across requests, reducing memory usage by up to 80% for million-token contexts; vAttention offers an alternative that allows swapping attention kernel implementations with minimal code changes.

### Continuous Batching & Disaggregated Serving
Serving infrastructure that dynamically batches incoming requests and separates prefill from decode phases across different hardware. Enables higher throughput by keeping GPUs fully utilized rather than waiting for the longest sequence in a batch; frameworks like vLLM, SGLang, and TensorRT-LLM implement this as the default serving paradigm.

### Structured Sparsity for Inference
Hardware-supported sparsity patterns (2:4, N:M) that skip zero-valued weights during matrix multiplication. NVIDIA Ampere and Hopper GPUs natively support 2:4 structured sparsity with near-2x speedups; combined with quantization, this enables running larger models on smaller hardware without quality-degrading unstructured pruning.

---

## Alignment & Safety

### Mechanistic Interpretability / Sparse Autoencoders
Techniques for understanding internal model representations by decomposing polysemantic neurons into interpretable features. Named a top 2026 breakthrough by MIT Technology Review; Anthropic's 2025 circuit tracing on Claude 3.5 Haiku surfaced mechanisms behind reasoning, hallucination, and jailbreak resistance; significant challenges remain, with SAE-reconstructed activations causing 10-40% performance degradation.

### Constitutional AI / RLAIF
Alignment through AI-generated feedback against a set of principles, rather than requiring human preference labels for every example. RLAIF has become a default method in the post-training literature since Anthropic's original paper; known risks include model collapse when training recursively on self-critic outputs, particularly with smaller models.

### Reward Model Overoptimization
The phenomenon where models learn to exploit reward model weaknesses rather than genuinely improving on the intended objective. A 2025 paper found that checklists outperform reward models for alignment in some settings; active research focuses on rubric-based approaches (OpenRubrics) and ensemble reward models to mitigate Goodhart's Law effects in RLHF.

### AI Evaluation Benchmarks (Frontier Safety)
Standardized tests for measuring dangerous capabilities (CBRN knowledge, autonomous replication, deception) and alignment properties. METR, NIST, and the UK AI Safety Institute are developing evaluation frameworks; the challenge is that benchmarks saturate quickly and adversarial evaluations require continuous updating to remain meaningful.

### Representation Engineering
Techniques for reading and writing to a model's internal representations to steer behavior without fine-tuning. Includes activation steering, concept erasure, and representation fine-tuning; offers a potentially more precise and efficient alternative to RLHF for controlling specific model behaviors, though robustness and generalization remain open questions.

---

## Agentic AI

### Model Context Protocol (MCP) & Agent-to-Agent (A2A)
Standardized protocols for connecting LLMs to tools and for agent-to-agent communication. MCP (Anthropic, 2024; governance transferred to Linux Foundation, 2025) provides a universal tool integration standard; A2A (Google) handles horizontal agent discovery and delegation via Agent Cards and REST endpoints; together they form the emerging infrastructure layer for multi-agent systems.

### Code Generation Agents (Cursor, Claude Code, Codex)
AI systems that write, edit, test, and deploy code with increasing autonomy. The 2025-2026 generation operates as persistent coding collaborators rather than completion engines, with capabilities including codebase-wide search, test execution, git operations, and multi-file refactoring; the competitive landscape includes Cursor, Claude Code, GitHub Copilot, and OpenAI Codex.

### Retrieval-Augmented Generation / GraphRAG
Enhancing LLM outputs by retrieving relevant information from external knowledge stores. GraphRAG (Microsoft, 2024) extracts entities and relationships into hierarchical community summaries, achieving 80% accuracy vs 50% for traditional RAG on complex queries; LazyGraphRAG (June 2025) reduces indexing cost to 0.1% of full GraphRAG.

### Multi-Agent Orchestration Frameworks
Platforms for coordinating multiple specialized AI agents on complex tasks. Key frameworks include LangGraph, CrewAI, AutoGen, Microsoft Agent Framework, and PydanticAI; the biggest model-level shift enabling these is "thinking" models that can plan, self-correct, and select tools more reliably than their predecessors.

### Computer Use / GUI Agents
AI systems that interact with graphical user interfaces by taking screenshots, clicking, and typing. Anthropic's computer use, OpenAI's Operator, and Google's Project Mariner represent different approaches to GUI automation; the technique extends agent capabilities to any software without requiring API integration.

---

## Multimodal & Generation

### Video Generation (Sora, Veo 3, Kling, Runway)
Diffusion-based systems generating video from text or image prompts with increasing physical realism. Veo 3 (Google DeepMind) was the first to natively generate video with synchronized audio; Sora 2 demonstrates cause-and-effect understanding; experts predict the first "generative video games" with real-time environment synthesis by 2026-2027.

### 3D Generation & World Models
Systems that generate or simulate 3D environments from text, images, or interaction data. Google's reported Veo 3.2 "Artemis" engine moves from pixel prediction toward physics simulation (gravity, fluid dynamics, object permanence); the definition of "world model" remains contested but the direction is toward persistent, interactive 3D environments generated on the fly.

### AI Music & Audio Generation
Models producing music, speech, and sound effects with increasing fidelity and control. Suno, Udio, and Stable Audio generate full songs from text prompts; ElevenLabs and others produce near-indistinguishable speech synthesis; the copyright and licensing implications remain legally unresolved in most jurisdictions.

### Neural Radiance Fields (NeRFs) & Gaussian Splatting
Techniques for reconstructing 3D scenes from 2D images. 3D Gaussian Splatting has largely overtaken NeRFs for real-time applications due to faster rendering; used in autonomous driving simulation, VR content creation, and digital twins; the transition from research to production tools accelerated through 2025.

---

## Hardware

### OpenAI Hardware Project (Titan / XPU)
Custom AI accelerators co-developed with a strategic silicon supplier and fabricated on advanced foundry nodes. Track production timing, dependence on NVIDIA GPUs for internal inference workloads, team composition, and second-generation roadmap signals.

### Groq LPU (Language Processing Unit)
A deterministic, single-core streaming architecture purpose-built for LLM inference, achieving consistent low-latency token generation. Groq raised $640M in Series D (2025), reaching $6.9B valuation; the architecture trades training flexibility for inference speed by eliminating memory bandwidth bottlenecks through on-chip SRAM.

### Neuromorphic / Photonic AI Chips
Brain-inspired and light-based computing architectures that offer order-of-magnitude improvements in energy efficiency. Photonic neuromorphic chips deliver 10-100x lower latency than electronic neuromorphic processors with 26x power savings over conventional SRAM-DAC architectures; the neuromorphic hardware market is projected to grow at 22% CAGR through 2026.

### NVIDIA Blackwell & Vera Rubin
NVIDIA's next-generation GPU architectures following Hopper. Blackwell (B200/GB200) is shipping in 2025 with native MoE support and FP4 inference; Vera Rubin (announced for 2026) is expected to further increase memory bandwidth and interconnect speed, maintaining NVIDIA's position as the default AI training platform.

### Custom Silicon Race (Google TPU, Amazon Trainium, Meta MTIA)
Hyperscalers designing proprietary AI accelerators to reduce costs and dependency on NVIDIA. Google's TPU v6e (Trillium) powers Gemini training; Amazon's Trainium2 targets price-competitive training and inference; Meta's MTIA is optimized for recommendation and ranking workloads; all reflect a trend toward vertically integrated AI infrastructure.

### RISC-V for AI Accelerators
The open-source instruction set architecture gaining traction as a substrate for custom AI chip design. Offers royalty-free, modifiable processor cores that can be purpose-built for specific inference workloads; Esperanto, Tenstorrent, and others are shipping RISC-V-based AI accelerators targeting edge and datacenter deployment.

---

## Non-AI Frontier Technology

### Augmented Reality Glasses
AI-integrated smart glasses entering a pivotal period with multiple competing products expected in 2026: Meta/Ray-Ban models with multimodal AI assistant access, Google/Samsung Android XR glasses with in-lens displays and Gemini integration, and Apple pursuing a lighter Vision Pro successor (2026-2028 window). The market grew 210% year-over-year in 2025 to reach $1.93B, projected at $8.26B by 2030.

### Brain-Computer Interfaces (Neuralink, Paradromics)
Implantable devices that translate neural signals into digital commands for communication and device control. Neuralink expanded to 21 human trial participants by late 2025 and plans high-volume automated production in 2026; Paradromics received FDA approval for its first long-term clinical trial as a competing approach; the technology currently serves patients with ALS and spinal cord injuries.

### Quantum Error Correction (IBM, Google, Microsoft)
The critical enabling technology for practical quantum computing, reducing logical error rates to levels needed for useful algorithms. IBM's Nighthawk processor (120 qubits) is on track for verified quantum advantage by end of 2026; Google's Willow chip achieved below-threshold error correction; IBM's Kookaburra (2026) will be the first processor using qLDPC memory for fault-tolerant operation.

### Nuclear Fusion Progress
Multiple approaches to fusion energy showing accelerating progress. China's EAST tokamak exceeded the Greenwald plasma density limit (a decades-old barrier); Tokamak Energy achieved 11.8 Tesla field strength with HTS magnets in a tokamak configuration; the National Ignition Facility reached 8.6 MJ laser delivery in April 2025 (4x the energy used for ignition); commercial timelines remain uncertain but the technical trajectory is advancing.

### AI-Driven Drug Discovery (AlphaFold 3, RFdiffusion, Boltz-2)
AI tools accelerating protein design and pharmaceutical development. AlphaFold 3 models protein-DNA-RNA-small molecule interactions at 50% better accuracy than traditional methods; Insilico Medicine's fully AI-designed drug completed Phase IIa trials in February 2026, developed in 18 months at ~$6M vs. the traditional $100-200M over 6-8 years; pharma AI R&D budgets projected to increase 75-85% over 2025 levels.

### Embodied AI / Humanoid Robotics
Foundation models enabling general-purpose robots that can be instructed in natural language rather than task-specific programming. China declared 2025 the "Year One of Humanoid Mass Production" with 140+ manufacturers and 330+ product models; 1X NEO opened consumer preorders in October 2025; RoboParty fully open-sourced its humanoid platform in January 2026; the efficiency gain from foundation models is estimated at 100x over traditional robot programming.

### Solid-State Batteries
Next-generation energy storage replacing liquid electrolytes with solid materials for higher energy density and safety. Toyota, Samsung SDI, and QuantumScape are targeting 2026-2028 for automotive-grade production; relevant to AI infrastructure because improved battery technology directly impacts edge AI device capability and robotics operational duration.

### Low-Earth Orbit Satellite Internet (Starlink, Kuiper, Lightspeed)
Mega-constellations providing global broadband coverage from orbit. Starlink operates 6,000+ satellites with direct-to-cell capability; Amazon Kuiper and Telesat Lightspeed are deploying competing networks; relevant to AI deployment because global connectivity enables distributed inference and data collection in regions without terrestrial infrastructure.

### Gene Editing (CRISPR Therapeutics)
Precision genome engineering entering commercial medical applications. The first CRISPR-based therapy (Casgevy for sickle cell disease) was approved in late 2023; ongoing trials for cancer, cardiovascular disease, and rare genetic disorders; AI is increasingly used to design guide RNAs and predict off-target effects, creating a convergence between AI and biotech development.

