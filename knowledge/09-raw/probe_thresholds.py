"""Calibrate the linking-cascade cosine thresholds (09-tools-and-prior-art.md §2).

Six mechanisms x three paraphrases: pairs inside a group are "one idea",
pairs across groups are "different mechanisms". Reports how well a plain cosine
threshold separates them, per embedding model, with and without corpus centering.

Run: python3 probe_thresholds.py   (needs sentence-transformers, downloads ~130MB once)
"""
import itertools
import numpy as np
from sentence_transformers import SentenceTransformer

GROUPS = {
    "cascade": [
        "A cheap proxy model filters most candidates before the expensive fitness evaluation.",
        "Two-stage screening: a low-cost surrogate discards weak candidates prior to full scoring.",
        "Cascaded evaluation prunes 70% of the population with a fast approximate scorer."],
    "island": [
        "Island models with periodic migration preserve population diversity.",
        "Splitting the population into subpopulations that exchange individuals every k generations keeps diversity.",
        "Multi-deme evolution with migration avoids premature convergence."],
    "early": [
        "Early stopping at 30% of the training budget saves GPU-hours with small accuracy loss.",
        "Terminating unpromising trials early frees compute for better configurations.",
        "Successive halving allocates budget to survivors and kills the rest early."],
    "mapel": [
        "MAP-Elites keeps an archive of elites per behavioural niche.",
        "Quality-diversity archives store the best solution for each behaviour descriptor cell.",
        "Illuminating the search space with a grid of niches yields diverse high-performing solutions."],
    "promptopt": [
        "Optimizing the prompt with textual gradients improves agent accuracy.",
        "Iteratively rewriting instructions based on failure feedback raises pipeline quality.",
        "Automatic prompt search replaces manual prompt engineering."],
    "memory": [
        "Storing past interactions as notes with keywords and links improves long-horizon recall.",
        "An agent memory that indexes prior episodes lets the model reuse earlier solutions.",
        "Retrieval over a note store beats stuffing the whole history into context."],
}
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "  # arctic-embed, query side only

LABELS, TEXTS = [], []
for g, ss in GROUPS.items():
    for s in ss:
        LABELS.append(g)
        TEXTS.append(s)


def split_pairs(E):
    E = E / np.linalg.norm(E, axis=1, keepdims=True)
    S = E @ E.T
    same = [float(S[i, j]) for i, j in itertools.combinations(range(len(TEXTS)), 2) if LABELS[i] == LABELS[j]]
    diff = [float(S[i, j]) for i, j in itertools.combinations(range(len(TEXTS)), 2) if LABELS[i] != LABELS[j]]
    return np.array(same), np.array(diff)


def report(tag, E):
    sa, di = split_pairs(E)
    best = max(((t, ((sa >= t).mean() + (di < t).mean()) / 2) for t in np.arange(-0.5, 0.99, 0.01)),
               key=lambda x: x[1])
    auc = np.mean([1.0 * (s > d) + 0.5 * (s == d) for s in sa for d in di])
    print(f"{tag:34} same med={np.median(sa):+.3f} min={sa.min():+.3f} | "
          f"diff med={np.median(di):+.3f} max={di.max():+.3f} | "
          f"best_t={best[0]:+.2f} bal_acc={best[1]:.2f} | AUC={auc:.3f}")
    return sa, di


def demo():
    for mid in ["Snowflake/snowflake-arctic-embed-s", "sentence-transformers/all-MiniLM-L6-v2"]:
        m = SentenceTransformer(mid)
        E = m.encode(TEXTS, normalize_embeddings=True)
        name = mid.split("/")[-1]
        sa, di = report(name, E)
        report(name + " +centered", E - E.mean(0))
        if name.startswith("snowflake"):
            print("  plan 08 thresholds on this model:")
            for t in (0.70, 0.92, 0.95):
                print(f"    cosine>={t}: same-idea caught {100 * (sa >= t).mean():5.1f}% | "
                      f"different-mechanism merged {100 * (di >= t).mean():5.1f}%")
            # the finding the doc rests on: the distributions overlap
            assert di.max() > sa.min(), "distributions separable -- rerun, §2 claim no longer holds"

    m = SentenceTransformer("Snowflake/snowflake-arctic-embed-s")
    docs = ["cascaded evaluation: cheap proxy filters candidates before expensive scoring",
            "island model with periodic migration maintains diversity"]
    qs = ["speed up candidate evaluation with a cheap proxy", "how to keep population diversity"]
    D = m.encode(docs, normalize_embeddings=True)
    with_p = m.encode([QUERY_PREFIX + q for q in qs], normalize_embeddings=True) @ D.T
    without = m.encode(qs, normalize_embeddings=True) @ D.T
    print(f"\nquery prefix effect: with={np.round(with_p, 3).tolist()} without={np.round(without, 3).tolist()}")
    print(f"  contrast (relevant - irrelevant): with={with_p[0, 0] - with_p[0, 1]:.3f} "
          f"without={without[0, 0] - without[0, 1]:.3f}")


if __name__ == "__main__":
    demo()

