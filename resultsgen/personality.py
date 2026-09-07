"""Assign one fixed, independently seeded personality to each application."""
from .config import config_dict
from .rng import Purpose, generator


def assign_personalities(cfg) -> list[str]:
    data = config_dict(cfg)
    weights = data["personality_weights"]
    names = list(weights)
    probabilities = [weights[name] / sum(weights.values()) for name in names]
    return [app["personality"] if app["personality"] != "random" else
            str(generator(data["seed"], Purpose.PERSONALITY, index).choice(names, p=probabilities))
            for index, app in enumerate(data["apps"])]
