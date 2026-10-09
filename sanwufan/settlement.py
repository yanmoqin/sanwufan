"""End-of-deal decisions. Actual tribute exchanges belong to the next phase."""
from dataclasses import dataclass
from .models import RuleViolation, validate_seat


@dataclass(frozen=True)
class Tribute:
    giver: int
    receiver: int


@dataclass(frozen=True)
class Settlement:
    defender_points: int
    winning_team: int
    next_dealer: int
    tribute_obligations: tuple[Tribute, ...]

    def to_dict(self) -> dict:
        return {
            "defender_points": self.defender_points,
            "winning_team": self.winning_team,
            "next_dealer": self.next_dealer,
            "tribute_obligations": [
                {"giver": t.giver, "receiver": t.receiver} for t in self.tribute_obligations
            ],
        }


def settle(dealer: int, defender_points: int) -> Settlement:
    """Seats 0/2 and 1/3 are partners; next seat is (seat + 1) % 4.

    Obligations are computed before next-deal exemptions are checked.
    """
    validate_seat(dealer)
    if type(defender_points) is not int or not 0 <= defender_points <= 100 or defender_points % 5:
        raise RuleViolation("INVALID_SCORE", "闲家得分必须是0到100之间的5的倍数")
    dealer_team = dealer % 2
    next_dealer = dealer if defender_points < 40 else (dealer + 1) % 4
    winning_team = dealer_team if defender_points < 40 else 1 - dealer_team
    if defender_points == 0 or defender_points >= 80:
        losing_team = 1 - winning_team
        tribute = tuple(Tribute(s, (s + 1) % 4) for s in range(4) if s % 2 == losing_team)
    elif defender_points >= 60:
        tribute = (Tribute(dealer, next_dealer),)
    else:
        tribute = ()
    return Settlement(defender_points, winning_team, next_dealer, tribute)


def settle_surrender(dealer: int, defender_points: int, initiator: int) -> Settlement:
    """Initiator's partnership loses; captured defender points select tribute tier."""
    validate_seat(initiator)
    settle(dealer, defender_points)  # Validate the score and dealer.
    losing = initiator % 2
    winning = 1 - losing
    next_dealer = dealer if winning == dealer % 2 else (dealer + 1) % 4
    if defender_points == 0 or defender_points >= 80:
        tribute = tuple(Tribute(s, (s + 1) % 4) for s in range(4) if s % 2 == losing)
    elif defender_points >= 60:
        giver = dealer if losing == dealer % 2 else initiator
        tribute = (Tribute(giver, (giver + 1) % 4),)
    else:
        tribute = ()
    return Settlement(defender_points, winning, next_dealer, tribute)
