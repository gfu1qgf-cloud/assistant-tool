"""Small bounded, pixel-space toy simulation; no Qt, threads or dependencies."""
from dataclasses import dataclass
import math
import random


@dataclass
class Ball:
    x: float
    y: float
    vx: float = 0.0
    vy: float = 0.0
    radius: float = 7.0
    resting: float = 0.0
    angle: float = 0.0


@dataclass
class Spark:
    x: float
    y: float
    vx: float
    vy: float
    life: float
    total: float


class BallWorld:
    def __init__(self, seed=None):
        self.balls, self.sparks = [], []
        self.random = random.Random(seed)

    def clear(self):
        self.balls.clear()
        self.sparks.clear()

    def active(self):
        return bool(self.balls or self.sparks)

    def spawn(self, x, y, bounds):
        if len(self.balls) >= 8:
            return False
        left, top, right, bottom = bounds
        if right - left < 16 or bottom - top < 16:
            return False
        self.balls.append(Ball(max(left + 8, min(right - 8, x)),
                               max(top + 8, min(bottom - 8, y))))
        return True

    @staticmethod
    def _contact(ball, nx, ny, penetration, dt):
        ball.x += nx * penetration
        ball.y += ny * penetration
        normal_speed = ball.vx * nx + ball.vy * ny
        if normal_speed < 0:
            restitution = .46 if normal_speed < -35 else 0.0
            ball.vx -= (1 + restitution) * normal_speed * nx
            ball.vy -= (1 + restitution) * normal_speed * ny
        tangent_speed = -ball.vx * ny + ball.vy * nx
        # Rolling resistance is a small *constant* opposing motion. Exponential
        # velocity damping made a downhill ball approach an artificial terminal
        # crawl instead of continuing to accelerate under gravity.
        friction = math.copysign(min(abs(tangent_speed), 24 * dt), tangent_speed)
        ball.vx += friction * ny
        ball.vy -= friction * nx

    def _explode(self, ball):
        for index in range(20):
            angle = index * math.tau / 20 + self.random.uniform(-.1, .1)
            speed = self.random.uniform(75, 190)
            life = self.random.uniform(.65, 1.05)
            self.sparks.append(Spark(ball.x, ball.y, math.cos(angle) * speed,
                                     math.sin(angle) * speed - 35, life, life))

    def advance(self, elapsed, segments, bounds):
        # Short fixed substeps prevent a fast ball tunnelling through thin lines.
        elapsed = max(0.0, min(.05, elapsed))
        if not elapsed:
            return
        count = max(1, math.ceil(elapsed * 240))
        dt = elapsed / count
        left, top, right, bottom = bounds
        surfaces = [*segments, ((left, bottom), (right, bottom))]
        for _ in range(count):
            retained = []
            for ball in self.balls:
                ball.vy = min(650, ball.vy + 900 * dt)
                ball.vx *= math.exp(-.02 * dt)  # Small air drag; gravity drives the slope.
                ball.x += ball.vx * dt
                ball.y += ball.vy * dt
                ball.angle += ball.vx * dt / ball.radius
                if ball.x < left + ball.radius:
                    ball.x = left + ball.radius
                    ball.vx = abs(ball.vx) * .4
                elif ball.x > right - ball.radius:
                    ball.x = right - ball.radius
                    ball.vx = -abs(ball.vx) * .4
                contact = None
                for (ax, ay), (bx, by) in surfaces:
                    if (ball.x + ball.radius < min(ax, bx) or ball.x - ball.radius > max(ax, bx)
                            or ball.y + ball.radius < min(ay, by)):
                        continue
                    dx, dy = bx - ax, by - ay
                    length2 = dx * dx + dy * dy
                    if length2 < 1e-9:
                        continue
                    fraction = max(0, min(1, ((ball.x - ax) * dx + (ball.y - ay) * dy) / length2))
                    qx, qy = ax + fraction * dx, ay + fraction * dy
                    nx, ny = ball.x - qx, ball.y - qy
                    distance = math.hypot(nx, ny)
                    if distance < ball.radius and ny <= 0:
                        if distance > 1e-9:
                            nx, ny = nx / distance, ny / distance
                        else:
                            nx, ny = 0.0, -1.0
                        penetration = ball.radius - distance + 1e-5
                        if contact is None or penetration > contact[2]:
                            contact = nx, ny, penetration
                supported = False
                if contact is not None:
                    nx, ny, penetration = contact
                    self._contact(ball, nx, ny, penetration, dt)
                    supported = ny < -.2
                # Numerical fallback at the lower edge; missing weather stays a gap.
                if ball.y > bottom - ball.radius:
                    ball.y = bottom - ball.radius
                    self._contact(ball, 0, -1, 0, dt)
                    supported = True
                ball.resting = ball.resting + dt if supported and math.hypot(ball.vx, ball.vy) < 14 else 0
                if ball.resting >= .4:
                    self._explode(ball)
                else:
                    retained.append(ball)
            self.balls = retained
            alive = []
            for spark in self.sparks:
                spark.life -= dt
                if spark.life > 0:
                    spark.vy += 260 * dt
                    spark.x += spark.vx * dt
                    spark.y += spark.vy * dt
                    alive.append(spark)
            self.sparks = alive
