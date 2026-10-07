"""Command-line rule and full-deal demonstrations."""
import argparse
import json
from . import Card, RuleContext, Suit, Trick, settle


def single_trick_demo():
    ctx = RuleContext(Suit.CLUBS)
    hands = tuple((Card.from_id(c),) for c in ("H:K", "H:4", "S:J", "J:big"))
    trick = Trick(ctx, hands, leader=0)
    for seat, hand in enumerate(hands):
        trick = trick.play(seat, hand)
    result = trick.result()
    print("固定牌局：红桃K → 红桃4 → 黑桃J毙牌 → 大王盖毙")
    print(f"本轮赢家：座位{result.winner}；收得{result.points}分；下一轮由座位{result.next_leader}领出")
    print("庄家座位0，闲家得60分时：")
    print(json.dumps(settle(0, 60).to_dict(), ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="三五反完整流程演示")
    parser.add_argument("--deals", type=int, default=3, help="演示局数，1至100")
    parser.add_argument("--seed", type=int, default=7, help="测试用固定随机种子")
    parser.add_argument("--single-trick", action="store_true", help="运行第一阶段单轮演示")
    args = parser.parse_args()
    if args.single_trick:
        single_trick_demo()
        return
    if not 1 <= args.deals <= 100:
        parser.error("--deals必须在1至100之间")
    from .demo import run_demo
    run_demo(args.deals, args.seed)


if __name__ == "__main__":
    main()
