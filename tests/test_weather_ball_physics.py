"""Pure-Python regression checks for the life assistant chart toy."""
import importlib.util
from pathlib import Path
import sys
import unittest

path = Path(__file__).resolve().parents[1] / "app_plugins" / "builtin" / "waste_reminder" / "weather_physics.py"
spec = importlib.util.spec_from_file_location("weather_ball_physics_under_test", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
Ball, BallWorld = module.Ball, module.BallWorld


class WeatherBallPhysicsTests(unittest.TestCase):
    def test_downhill_velocity_increases_after_settling(self):
        world = BallWorld(seed=5)
        bounds = (0, 0, 400, 220)
        slope = [((0, 40), (400, 160))]
        world.balls.append(Ball(90, 65, radius=7))
        speeds = []
        for frame in range(60):
            world.advance(1 / 60, slope, bounds)
            if frame in (8, 30, 52):
                speeds.append(world.balls[0].vx)
        self.assertGreater(speeds[0], 0)
        self.assertGreater(speeds[1], speeds[0] + 15)
        self.assertGreater(speeds[2], speeds[1] + 15)

    def test_flat_surface_still_rests_and_explodes(self):
        world = BallWorld(seed=2)
        world.spawn(120, 15, (0, 0, 300, 180))
        exploded = False
        for _ in range(600):
            world.advance(1 / 60, [((0, 100), (300, 100))], (0, 0, 300, 180))
            exploded |= bool(world.sparks)
        self.assertTrue(exploded)
        self.assertFalse(world.active())


if __name__ == "__main__":
    unittest.main()
