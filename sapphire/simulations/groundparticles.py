"""Perform simulations of CORSIKA air showers on a cluster of stations

This simulation uses a HDF5 file created from a CORSIKA simulation with
the `store_corsika_data` script. The shower is 'thrown' on the cluster
with random core positions and azimuth angles.

Example usage::

    import tables

    from sapphire.simulations.groundparticles import GroundParticlesSimulation
    from sapphire.clusters import ScienceParkCluster

    data = tables.open_file('/tmp/test_groundparticle_simulation.h5', 'w')
    cluster = ScienceParkCluster()

    sim = GroundParticlesSimulation('corsika.h5', 500, cluster, data, '/', 10)
    sim.run()

"""
from math import pi, sin, cos, sqrt
import warnings
import random

import numpy as np
import tables

from .detector import HiSPARCSimulation
from ..utils import pbar


class GroundParticlesSimulation(HiSPARCSimulation):

    def __init__(self, corsikafile_path, max_core_distance, *args, **kwargs):
        super(GroundParticlesSimulation, self).__init__(*args, **kwargs)

        self.corsikafile = tables.open_file(corsikafile_path, 'r')
        self.groundparticles = self.corsikafile.get_node('/groundparticles')
        self.max_core_distance = max_core_distance

        for station in self.cluster.stations:
            station.gps_offset = self.simulate_station_offset()
            station.detector_offsets = self.simulate_detector_offsets(
                len(station.detectors))

        # Store updated version of the cluster
        self.coincidence_group._v_attrs.cluster = self.cluster

    def __del__(self):
        self.finish()

    def finish(self):
        """Clean-up after simulation"""

        self.corsikafile.close()

    def generate_shower_parameters(self):
        """Generate shower parameters like core position, energy, etc.

        For this groundparticles simulation, only the shower core position
        and rotation angle of the shower are generated.  Do *not*
        interpret these parameters as the position of the cluster, or the
        rotation of the cluster!  Interpret them as *shower* parameters.

        :returns: dictionary with shower parameters: core_pos
                  (x, y-tuple) and azimuth.

        """
        R = self.max_core_distance

        event_header = self.groundparticles._v_attrs['event_header']
        event_end = self.groundparticles._v_attrs['event_end']
        corsika_parameters = {'zenith': event_header.zenith,
                              'size': event_end.n_electrons_levels,
                              'energy': event_header.energy,
                              'particle': event_header.particle}

        # DF: This is the fastest implementation I could come up with.  I
        # timed several permutations of numpy / math, and tried a Monte
        # Carlo method in which I pick x and y in a square (fast) and then
        # determine if they fall inside a circle or not (surprisingly
        # slow, because of an if-statement, and despite some optimizations
        # suggested by HM).

        for i in pbar(range(self.N)):
            r = sqrt(np.random.uniform(0, R ** 2))
            phi = np.random.uniform(-pi, pi)
            alpha = np.random.uniform(-pi, pi)

            x = r * cos(phi)
            y = r * sin(phi)

            # Add Corsika shower azimuth to generated alpha
            # and make them fit in (-pi, pi].
            shower_azimuth = alpha + event_header.azimuth
            if shower_azimuth > pi:
                shower_azimuth -= 2 * pi
            elif shower_azimuth <= -pi:
                shower_azimuth += 2 * pi

            shower_parameters = {'ext_timestamp': (long(1e9) + i) * long(1e9),
                                 'core_pos': (x, y),
                                 'azimuth': shower_azimuth}
            shower_parameters.update(corsika_parameters)
            self._prepare_cluster_for_shower(x, y, alpha)

            yield shower_parameters

    def _prepare_cluster_for_shower(self, x, y, alpha):
        """Prepare the cluster object for the simulation of a shower.

        Rotate and translate the cluster so that (0, 0) coincides with the
        shower core position and 'East' coincides with the shower azimuth
        direction.

        """
        # rotate the core position around the original cluster center
        xp = x * cos(-alpha) - y * sin(-alpha)
        yp = x * sin(-alpha) + y * cos(-alpha)

        self.cluster.set_coordinates(-xp, -yp, 0, -alpha)

    def simulate_detector_response(self, detector, shower_parameters):
        """Simulate detector response to a shower.

        Checks if leptons have passed a detector. If so, it returns the number
        of leptons in the detector and the arrival time of the first lepton
        passing the detector.

        The detector is approximated by a square with a surface of 0.5
        square meter which is *not* correctly rotated.  In fact, during
        the simulation, the rotation of the detector is undefined.  This
        might be faster than a more thorough implementation.

        """
        detector_boundary = 0.3535534
        x, y = detector.get_xy_coordinates()

        # particle ids 2, 3, 5, 6 are electrons and muons, and id 4 is no
        # longer used (were neutrino's).
        query = ('(x >= %f) & (x <= %f) & (y >= %f) & (y <= %f)'
                 ' & (particle_id >= 2) & (particle_id <= 6)' %
                 (x - detector_boundary, x + detector_boundary,
                  y - detector_boundary, x + detector_boundary))
        detected = [row['t'] for row in self.groundparticles.where(query)]

        if detected:
            n_detected = len(detected)
            transporttimes = self.simulate_signal_transport_time(n_detected)
            for i in range(n_detected):
                detected[i] += transporttimes[i]
            observables = {'n': n_detected, 't': min(detected)}
        else:
            observables = {'n': 0., 't': -999}

        return observables

    def simulate_trigger(self, detector_observables):
        """Simulate a trigger response.

        :returns: True if at least 2 detectors detect at least one particle,
                  False otherwise.

        """
        detectors_hit = [True for observables in detector_observables
                         if observables['n'] > 0]

        if sum(detectors_hit) >= 2:
            return True
        else:
            return False

    def simulate_gps(self, station_observables, shower_parameters, station):
        """Simulate gps timestamp."""

        arrival_times = [station_observables['t%d' % id]
                         for id in range(1, 5)
                         if station_observables.get('n%d' % id, -1) > 0]

        if len(arrival_times) > 1:
            trigger_time = sorted(arrival_times)[1]

            ext_timestamp = shower_parameters['ext_timestamp']
            ext_timestamp += int(trigger_time + station.gps_offset +
                                self.simulate_gps_uncertainty())
            timestamp = int(ext_timestamp / long(1e9))
            nanoseconds = int(ext_timestamp % long(1e9))

            gps_timestamp = {'ext_timestamp': ext_timestamp,
                             'timestamp': timestamp, 'nanoseconds': nanoseconds}
            station_observables.update(gps_timestamp)

        return station_observables


class DetectorBoundarySimulation(GroundParticlesSimulation):

    """ More accuratly simulate the detection area of the detectors.

    This requires a slightly more complex query which is a bit slower.

    """

    def simulate_detector_response(self, detector, shower_parameters):
        """Simulate the detector detection area accurately.

        First particles are filtered to see which fall inside a
        non-rotated square box around the detector (i.e. sides of 1.2m).
        For the remaining particles a more accurate query is used to see
        which actually hit the detector. The advantage of using the
        square is that column indexes can be used, which may speed up
        queries.

        """
        detector_boundary = 0.6

        x, y = detector.get_xy_coordinates()
        c = detector.get_corners()

        b11, line1, b12 = self.get_line_boundary_eqs(c[0], c[1], c[2])
        b21, line2, b22 = self.get_line_boundary_eqs(c[1], c[2], c[3])
        query = ("(x >= %f) & (x <= %f) & (y >= %f) & (y <= %f) & "
                 "(b11 < %s) & (%s < b12) & (b21 < %s) & (%s < b22) & "
                 "(particle_id >= 2) & (particle_id <= 6)" %
                 (x - detector_boundary, x + detector_boundary,
                  y - detector_boundary, x + detector_boundary,
                  line1, line1, line2, line2))
        detected = [row['t'] for row in self.groundparticles.where(query)]

        if detected:
            n_detected = len(detected)
            transporttimes = self.simulate_signal_transport_time(n_detected)
            for i in range(n_detected):
                detected[i] += transporttimes[i]
            observables = {'n': n_detected, 't': min(detected)}
        else:
            observables = {'n': 0., 't': -999}

        return observables

    def get_line_boundary_eqs(self, p0, p1, p2):
        """Get line equations using three points

        Given three points, this function computes the equations for two
        parallel lines going through these points.  The first and second
        point are on the same line, whereas the third point is taken to
        be on a line which runs parallel to the first.  The return value
        is an equation and two boundaries which can be used to test if a
        point is between the two lines.

        :param p0, p1: (x, y) tuples on the same line
        :param p2: (x, y) tuple on the parallel line

        :return: value1, equation, value2, such that points satisfying
            value1 < equation < value2 are between the parallel lines

        Example::

            >>> get_line_boundary_eqs((0, 0), (1, 1), (0, 2))
            (0.0, 'y - 1.000000 * x', 2.0)

        """
        (x0, y0), (x1, y1), (x2, y2) = p0, p1, p2

        # Compute the general equation for the lines
        if not (x0 == x1):
            # First, compute the slope
            a = (y1 - y0) / (x1 - x0)

            # Calculate the y-intercepts of both lines
            b1 = y0 - a * x0
            b2 = y2 - a * x2

            line = "y - %f * x" % a
        else:
            # line is exactly vertical
            line = "x"
            b1, b2 = x0, x2

        # And order the y-intercepts
        if b1 > b2:
            b1, b2 = b2, b1

        return b1, line, b2


class DetectorSignalSimulation(GroundParticlesSimulation):

    """ More accuratly simulate the detection area of the detectors.

    This requires a slightly more complex query which is a bit slower.

    """

    def simulate_detector_response(self, detector, shower_parameters):
        """Simulate the detector detection area accurately.

        First particles are filtered to see which fall inside a
        non-rotated square box around the detector (i.e. sides of 1.2m).
        For the remaining particles a more accurate query is used to see
        which actually hit the detector. The advantage of using the
        square is that column indexes can be used, which may speed up
        queries.

        """
        detector_boundary = 0.3535534
        x, y = detector.get_xy_coordinates()

        # particle ids 2, 3, 5, 6 are electrons and muons, and id 4 is no
        # longer used (were neutrino's).
        query = ('(x >= %f) & (x <= %f) & (y >= %f) & (y <= %f)'
                 ' & (particle_id >= 2) & (particle_id <= 6)' %
                 (x - detector_boundary, x + detector_boundary,
                  y - detector_boundary, x + detector_boundary))
        detected = [[row['t'], row['p_x'], row['p_y'], row['p_z']]
                    for row in self.groundparticles.where(query)]
        if detected:
            particle_momenta = ((p[1], p[2], p[3]) for p in detected)
            mips = self.simulate_detector_mips(particle_momenta)

            # simulation of transport times

            n_detected = len(detected)
            transporttimes = self.simulate_signal_transport_time(n_detected)
            for i in range(n_detected):
                detected[i][0] += transporttimes[i]
            observables = {'n': mips, 't': np.min(detected, 0)[0]}
        else:
            observables = {'n': 0., 't': -999}

        return observables
