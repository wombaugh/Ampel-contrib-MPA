#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File              : ampel/contrib/hu/t0/DecentFilter.py
# License           : BSD-3-Clause
# Author            : m. giomi <matteo.giomi@desy.de>
# Date              : 06.06.2018
# Last Modified Date: 27.06.2018
# Last Modified By  : m. giomi <matteo.giomi@desy.de>

from numpy import exp, array
import logging
from urllib.parse import urlparse
from astropy.coordinates import SkyCoord
from astropy.table import Table
import sys
try:
	from catsHTM import cone_search
	doCat = True
except ImportError:
	doCat = False
from ampel.base.abstract.AbsAlertFilter import AbsAlertFilter

class MPALensingFilter(AbsAlertFilter):
	"""
                Modification of the Decent filter to only accept candidates with PS match (potential lens).

		General-purpose filter with ~ 0.6% acceptance. It selects alerts based on:
			* numper of previous detections
			* positive subtraction flag
			* loose cuts on image quality (fwhm, elongation, number of bad pixels, and the
			difference between PSF and aperture magnitude)
			* distance to known SS objects
			* real-bogus
			* detection of proper-motion and paralax for coincidence sources in GAIA DR2
		
		The filter has a very weak dependence on the real-bogus score and it is independent
		on the provided PS1 star-galaxy classification.
	"""

	# Static version info
	version = 1.0
	resources = ('catsHTM.default',)

	def __init__(self, on_match_t2_units, base_config=None, run_config=None, logger=None):
		"""
		"""
		if run_config is None or len(run_config) == 0:
			raise ValueError("Please check you run configuration")

		self.on_match_t2_units = on_match_t2_units
		self.logger = logger if logger is not None else logging.getLogger()
		
		config_params = (
			'MIN_NDET',					# number of previous detections
			'MIN_TSPAN', 				# minimum duration of alert detection history [days]
			'MAX_TSPAN', 				# maximum duration of alert detection history [days]
			'MIN_RB',					# real bogus score
			'MAX_FWHM',					# sexctrator FWHM (assume Gaussian) [pix]
			'MAX_ELONG',				# Axis ratio of image: aimage / bimage 
			'MAX_MAGDIFF',				# Difference: magap - magpsf [mag]
			'MAX_NBAD',					# number of bad pixels in a 5 x 5 pixel stamp
			'MIN_DIST_TO_SSO',			# distance to nearest solar system object [arcsec]
			'MIN_GAL_LAT', 				# minium distance from galactic plane. Set to negative to disable cut.
			'GAIA_RS',					# search radius for GAIA DR2 matching [arcsec]
			'GAIA_PM_SIGNIF',			# significance of proper motion detection of GAIA counterpart [sigma]
			'GAIA_PLX_SIGNIF',			# significance of parallax detection of GAIA counterpart [sigma]
			'GAIA_VETO_GMAG_MIN', 		        # min gmag for normalized distance cut of GAIA counterparts [mag]
			'GAIA_VETO_GMAG_MAX', 		        # max gmag for normalized distance cut of GAIA counterparts [mag]
			'PS1_SGVETO_RAD',			# maximum distance to closest PS1 source for SG score veto [arcsec]
			'PS1_SGVETO_SGTH',			# maximum allowed SG score for PS1 source within PS1_SGVETO_RAD
                        'PS1_MAXIMUM_DIST'                      # Maximum distance of closest PS1 match
			)
		for el in config_params:
			if el not in run_config:
				raise ValueError("Parameter %s missing, please check your channel config" % el)
			if run_config[el] is None:
				raise ValueError("Parameter %s is None, please check your channel config" % el)
			self.logger.info("Using %s=%s" % (el, run_config[el]))
		
		
		# ----- set filter proerties ----- #
		
		# history
		self.min_ndet 					= run_config['MIN_NDET'] 
		self.min_tspan					= run_config['MIN_TSPAN']
		self.max_tspan					= run_config['MAX_TSPAN']
		
		# Image quality
		self.max_fwhm					= run_config['MAX_FWHM']
		self.max_elong					= run_config['MAX_ELONG']
		self.max_magdiff				= run_config['MAX_MAGDIFF']
		self.max_nbad					= run_config['MAX_NBAD']
		self.min_rb						= run_config['MIN_RB']
		
		# astro
		self.min_ssdistnr	 			= run_config['MIN_DIST_TO_SSO']
		self.min_gal_lat				= run_config['MIN_GAL_LAT']
		self.gaia_rs					= run_config['GAIA_RS']
		self.gaia_pm_signif				= run_config['GAIA_PM_SIGNIF']
		self.gaia_plx_signif			= run_config['GAIA_PLX_SIGNIF']
		self.gaia_veto_gmag_min			= run_config['GAIA_VETO_GMAG_MIN']
		self.gaia_veto_gmag_max			= run_config['GAIA_VETO_GMAG_MAX']
		self.ps1_sgveto_rad				= run_config['PS1_SGVETO_RAD']
		self.ps1_sgveto_th				= run_config['PS1_SGVETO_SGTH']
		self.ps1_max_dist			= run_config['PS1_MAXIMUM_DIST']
                
		# technical
		if doCat:
			self.catshtm_path 			= urlparse(base_config['catsHTM.default']).path
			self.logger.info("using catsHTM files in %s"%self.catshtm_path)
		self.keys_to_check = (
			'fwhm', 'elong', 'magdiff', 'nbad', 'distpsnr1', 'sgscore1', 'distpsnr2', 
			'sgscore2', 'distpsnr3', 'sgscore3', 'isdiffpos', 'ra', 'dec', 'rb', 'ssdistnr')


	def _alert_has_keys(self, photop):
		"""
			check that given photopoint contains all the keys needed to filter
		"""
		for el in self.keys_to_check:
			if el not in photop:
				self.logger.debug("rejected: '%s' missing" % el)
				return False
			if photop[el] is None:
				self.logger.debug("rejected: '%s' is None" % el)
				return False
		return True


	def get_galactic_latitude(self, transient):
		"""
			compute galactic latitude of the transient
		"""
		coordinates = SkyCoord(transient['ra'], transient['dec'], unit='deg')
		b = coordinates.galactic.b.deg
		return b


	def is_star_in_PS1(self, transient):
		"""
			apply combined cut on sgscore1 and distpsnr1 to reject the transient if
			there is a PS1 star-like object in it's immediate vicinity
		"""
		if (transient['distpsnr1'] < self.ps1_sgveto_rad and
				transient['sgscore1'] > self.ps1_sgveto_th):
			return True
		else:
			return False




	def is_star_in_gaia(self, transient):
		"""
			match tranient position with GAIA DR2 and uses parallax 
			and proper motion to evaluate star-likeliness
			
			returns: True (is a star) or False otehrwise.
		"""
		
		transient_coords = SkyCoord(transient['ra'], transient['dec'], unit='deg')
		srcs, colnames, colunits = cone_search(
											'GAIADR2',
											transient_coords.ra.rad, transient_coords.dec.rad,
											self.gaia_rs,
											catalogs_dir=self.catshtm_path)
		my_keys = ['RA', 'Dec', 'Mag_G', 'PMRA', 'ErrPMRA', 'PMDec', 'ErrPMDec', 'Plx', 'ErrPlx']
		if len(srcs) > 0:
			gaia_tab					= Table(srcs, names=colnames)
			gaia_tab					= gaia_tab[my_keys]
			gaia_coords					= SkyCoord(gaia_tab['RA'], gaia_tab['Dec'], unit='rad')
			
			# compute distance
			gaia_tab['DISTANCE']		= transient_coords.separation(gaia_coords).arcsec
			gaia_tab['DISTANCE_NORM']	= (
				1.8 + 0.6 * exp( (20 - gaia_tab['Mag_G']) / 2.05) > gaia_tab['DISTANCE'])	#TODO: vary mag_G exclusion
			gaia_tab['FLAG_PROX']		= [
											True if x['DISTANCE_NORM'] == True and 
											(self.gaia_veto_gmag_min <= x['Mag_G'] <= self.gaia_veto_gmag_max) else 
											False for x in gaia_tab
											]

			# check for proper motion and parallax conditioned to distance
			gaia_tab['FLAG_PMRA']		= abs(gaia_tab['PMRA']  / gaia_tab['ErrPMRA']) > self.gaia_pm_signif
			gaia_tab['FLAG_PMDec']		= abs(gaia_tab['PMDec'] / gaia_tab['ErrPMDec']) > self.gaia_pm_signif
			gaia_tab['FLAG_Plx']		= abs(gaia_tab['Plx']   / gaia_tab['ErrPlx']) > self.gaia_plx_signif
			if (any(gaia_tab['FLAG_PMRA'] == True) or 
				any(gaia_tab['FLAG_PMDec'] == True) or
				any(gaia_tab['FLAG_Plx'] == True)) and any(gaia_tab['FLAG_PROX'] == True):
				return True
		return False


	def apply(self, alert):
		"""
		Mandatory implementation.
		To exclude the alert, return *None*
		To accept it, either return
			* self.on_match_t2_units
			* or a custom combination of T2 unit names
		"""
		
		# --------------------------------------------------------------------- #
		#					CUT ON THE HISTORY OF THE ALERT						#
		# --------------------------------------------------------------------- #
		
		npp = len(alert.pps)
		if npp < self.min_ndet:
			self.logger.debug("rejected: %d photopoints in alert (minimum required %d)"% 
				(npp, self.min_ndet))
			return None
		
		# cut on length of detection history
		detections_jds = alert.get_values('jd', upper_limits=False)
		det_tspan = max(detections_jds) - min(detections_jds)
		if not (self.min_tspan < det_tspan < self.max_tspan):
			self.logger.debug("rejected: detection history is %.3f d long, requested between %.3f and %.3f d"%
				(det_tspan, self.min_tspan, self.max_tspan))
			return None
		
		# --------------------------------------------------------------------- #
		#							IMAGE QUALITY CUTS							#
		# --------------------------------------------------------------------- #
		
		latest = alert.pps[0]
		if not self._alert_has_keys(latest):
			return None
		
		if (latest['isdiffpos'] == 'f' or latest['isdiffpos'] == '0'):
			self.logger.debug("rejected: 'isdiffpos' is %s", latest['isdiffpos'])
			return None
		
		if latest['rb'] < self.min_rb:
			self.logger.debug("rejected: RB score %.2f below threshod (%.2f)"%
				(latest['rb'], self.min_rb))
			return None
		
		if latest['fwhm'] > self.max_fwhm:
			self.logger.debug("rejected: fwhm %.2f above threshod (%.2f)"%
				(latest['fwhm'], self.max_fwhm))
			return None
		
		if latest['elong'] > self.max_elong:
			self.logger.debug("rejected: elongation %.2f above threshod (%.2f)"%
				(latest['elong'], self.max_elong))
			return None
		
		if abs(latest['magdiff']) > self.max_magdiff:
			self.logger.debug("rejected: magdiff (AP-PSF) %.2f above threshod (%.2f)"% 
				(latest['magdiff'], self.max_magdiff))
			return None
		
		# --------------------------------------------------------------------- #
		#								ASTRONOMY								#
		# --------------------------------------------------------------------- #
		
		# check for closeby ss objects
		if (0 < latest['ssdistnr'] < self.min_ssdistnr):
			self.logger.debug("rejected: solar-system object close to transient (max allowed: %d)."%
				(self.min_ssdistnr))
			return None

                # check that a ps1 counterpart exists
#		if latest['distpsnr1'] < self.ps1_max_dist and latest['distpsnr1']>0:
                        # Nearby counterpart exists
#                        pass
#                else:
#                        self.logger.debug("rejected: no nearby PS object detected (min distance: %f)."%
#                                (self.p1_max_dist))
#                        return None
                
		if latest.get('distpsnr1',666) > self.ps1_max_dist:
			self.logger.debug("rejected: no PS1 object within  %.2f arcsec from alert."%
				(self.ps1_max_dist))
			return None

                
		# cut on galactic latitude
		b = self.get_galactic_latitude(latest)
		if abs(b) < self.min_gal_lat:
			self.logger.debug("rejected: b=%.4f, too close to Galactic plane (max allowed: %f)."%
				(b, self.min_gal_lat))
			return None
		
		# check ps1 star-galaxy score
		if self.is_star_in_PS1(latest):
			self.logger.debug("rejected: closest PS1 source %.2f arcsec away with sgscore of %.2f"%
				(latest['distpsnr1'], latest['sgscore1']))
			return None
		
		
		# check with gaia
		if self.gaia_rs>0:
			if not doCat:
				sys.exit("Cannot match to Gaia without catsHTM!")	
			if self.is_star_in_gaia(latest):
				self.logger.debug("rejected: within %.2f arcsec from a GAIA start (PM of PLX)" % 
					(self.gaia_rs))
				return None
			
		# congratulation alert! you made it!
		self.logger.debug("Alert %s accepted. Latest pp ID: %d"%(alert.tran_id, latest['candid']))
		for key in self.keys_to_check:
			self.logger.debug("{}: {}".format(key, latest[key]))
		return self.on_match_t2_units
	
