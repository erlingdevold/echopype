import numpy as np
from scipy import signal
import xarray as xr
from ..utils import uwa
from .calibrate_base import CalibrateBase, CAL_PARAMS


class CalibrateEK(CalibrateBase):

    def __init__(self, echodata):
        super().__init__(echodata)

        # cal params specific to EK echosounders
        self.cal_params = dict.fromkeys(CAL_PARAMS['EK'])

    def calc_range_meter(self, waveform_mode, tvg_correction_factor):
        """
        Parameters
        ----------
        waveform_mode : str
            ``CW`` for CW-mode samples, either recorded as complex or power samples
            ``BB`` for BB-mode samples, recorded as complex samples
        tvg_correction_factor : int
            - 2 for CW-mode power samples
            - 0 for CW-mode complex samples

        Returns
        -------
        range_meter : xr.DataArray
            range in units meter
        """
        if waveform_mode == 'CW':
            sample_thickness = self.echodata.raw_beam['sample_interval'] * self.env_params['sound_speed'] / 2
            # TODO: Check with the AFSC about the half sample difference
            range_meter = (self.echodata.raw_beam.range_bin
                           - tvg_correction_factor) * sample_thickness  # [frequency x range_bin]
        elif waveform_mode == 'BB':
            shift = self.echodata.raw_beam['transmit_duration_nominal']  # based on Lar Anderson's Matlab code
            # TODO: once we allow putting in arbitrary sound_speed, change below to use linearly-interpolated values
            range_meter = ((self.echodata.raw_beam.range_bin * self.echodata.raw_beam['sample_interval'] - shift)
                           * self.env_params['sound_speed'].reindex({'ping_time': self.echodata.raw_beam.ping_time},
                                                                    'nearest') / 2)
            # TODO: Lar Anderson's code include a slicing by minRange with a default of 0.02 m,
            #  need to ask why and see if necessary here
        else:
            raise ValueError('Input waveform_mode not recognized!')

        # make order of dims conform with the order of backscatter data
        range_meter = range_meter.transpose('frequency', 'ping_time', 'range_bin')
        range_meter.name = 'range'  # add name to facilitate xr.merge

        return range_meter

    def _get_vend_cal_params_power(self, param):
        """Get cal parameters stored in the Vendor group.

        Parameters
        ----------
        param : str
            name of parameter to retrieve
        """
        # TODO: need to test with EK80 power/angle data
        #  currently this has only been tested with EK60 data
        ds_vend = self.echodata.raw_vend

        if param not in ds_vend:
            return None

        if param not in ['sa_correction', 'gain_correction']:
            raise ValueError(f"Unknown parameter {param}")

        # Drop NaN ping_time for transmit_duration_nominal
        if np.any(np.isnan(self.echodata.raw_beam['transmit_duration_nominal'])):
            # TODO: resolve performance warning:
            #  /Users/wu-jung/miniconda3/envs/echopype_jan2021/lib/python3.8/site-packages/xarray/core/indexing.py:1369:
            #  PerformanceWarning: Slicing is producing a large chunk. To accept the large
            #  chunk and silence this warning, set the option
            #      >>> with dask.config.set(**{'array.slicing.split_large_chunks': False}):
            #      ...     array[indexer]
            #  To avoid creating the large chunks, set the option
            #      >>> with dask.config.set(**{'array.slicing.split_large_chunks': True}):
            #      ...     array[indexer]
            #    return self.array[key]
            self.echodata.raw_beam = self.echodata.raw_beam.dropna(dim='ping_time', how='any',
                                                                   subset=['transmit_duration_nominal'])

        # Find index with correct pulse length
        unique_pulse_length = np.unique(self.echodata.raw_beam['transmit_duration_nominal'], axis=1)
        idx_wanted = np.abs(ds_vend['pulse_length'] - unique_pulse_length).argmin(dim='pulse_length_bin')

        return ds_vend[param].sel(pulse_length_bin=idx_wanted).drop('pulse_length_bin')

    def get_cal_params(self, cal_params):
        """Get cal params using user inputs or values from data file.

        Parameters
        ----------
        cal_params : dict
        """
        # Params from the Vendor group
        params_from_vend = ['sa_correction', 'gain_correction']
        for p in params_from_vend:
            # substitute if None in user input
            self.cal_params[p] = cal_params[p] if p in cal_params else self._get_vend_cal_params_power(p)

        # Other params
        self.cal_params['equivalent_beam_angle'] = (cal_params['equivalent_beam_angle']
                                                    if 'equivalent_beam_angle' in cal_params
                                                    else self.echodata.raw_beam['equivalent_beam_angle'])

    def _cal_power(self, cal_type, use_raw_beam_power=False):
        """Calibrate power data from EK60 and EK80.

        Parameters
        ----------
        cal_type : str
            'Sv' for calculating volume backscattering strength, or
            'Sp' for calculating point backscattering strength
        use_raw_beam_power : bool
            whether to use raw_beam_power.
            If True use echodata.raw_beam; if False use echodata.raw_beam_power.
            Note raw_beam_power could only exist for EK80 data.

        Returns
        -------
        Sv or Sp
        """
        # Select source of backscatter data
        if use_raw_beam_power:
            raw_beam = self.echodata.raw_beam_power
        else:
            raw_beam = self.echodata.raw_beam

        # Derived params
        wavelength = self.env_params['sound_speed'] / raw_beam['frequency']  # wavelength
        range_meter = self.range_meter

        # Transmission loss
        spreading_loss = 20 * np.log10(range_meter.where(range_meter >= 1, other=1))
        absorption_loss = 2 * self.env_params['sound_absorption'] * range_meter

        if cal_type == 'Sv':
            # Calc gain
            CSv = (10 * np.log10(raw_beam['transmit_power'])
                   + 2 * self.cal_params['gain_correction']
                   + self.cal_params['equivalent_beam_angle']
                   + 10 * np.log10(wavelength ** 2
                                   * raw_beam['transmit_duration_nominal']
                                   * self.env_params['sound_speed']
                                   / (32 * np.pi ** 2)))

            # Calibration and echo integration
            out = (raw_beam['backscatter_r']
                   + spreading_loss + absorption_loss
                   - CSv - 2 * self.cal_params['sa_correction'])
            out.name = 'Sv'

        elif cal_type == 'Sp':
            # Calc gain
            CSp = (10 * np.log10(raw_beam['transmit_power'])
                   + 2 * self.cal_params['gain_correction']
                   + 10 * np.log10(wavelength ** 2 / (16 * np.pi ** 2)))

            # Calibration and echo integration
            out = (raw_beam.backscatter_r
                   + spreading_loss * 2 + absorption_loss
                   - CSp)
            out.name = 'Sp'
        else:
            raise ValueError('cal_type not recognized!')

        # Attach calculated range (with units meter) into data set
        out = out.to_dataset()
        out = out.merge(range_meter)

        return out


class CalibrateEK60(CalibrateEK):

    def __init__(self, echodata):
        super().__init__(echodata)

        # default to CW mode recorded as power samples
        self.range_meter = self.calc_range_meter(waveform_mode='CW', tvg_correction_factor=0)

    def get_env_params(self, env_params):
        """Get env params using user inputs or values from data file.

        EK60 file by default contains only sound speed and absorption.
        In cases when temperature, salinity, and pressure values are supplied
        by the user simultaneously, the sound speed and absorption are re-calculated.

        Parameters
        ----------
        env_params : dict
        """
        # Re-calculate environment parameters if user supply all env variables
        if ('temperature' in env_params) and ('salinity' in env_params) and ('pressure' in env_params):
            for p in ['temperature', 'salinity', 'pressure']:
                self.env_params[p] = env_params[p]
            self.env_params['sound_speed'] = uwa.calc_sound_speed(temperature=self.env_params['temperature'],
                                                                  salinity=self.env_params['salinity'],
                                                                  pressure=self.env_params['pressure'])
            self.env_params['sound_absorption'] = uwa.calc_absorption(frequency=self.echodata.raw_beam['frequency'],
                                                                      temperature=self.env_params['temperature'],
                                                                      salinity=self.env_params['salinity'],
                                                                      pressure=self.env_params['pressure'])
        # Otherwise get sound speed and absorption from user inputs or raw data file
        else:
            self.env_params['sound_speed'] = (env_params['sound_speed']
                                              if 'sound_speed' in env_params
                                              else self.echodata.raw_env['sound_speed_indicative'])
            self.env_params['sound_absorption'] = (env_params['sound_absorption']
                                                   if 'sound_absorption' in env_params
                                                   else self.echodata.raw_env['absorption_indicative'])

    def get_Sv(self):
        return self._cal_power(cal_type='Sv')

    def get_Sp(self):
        return self._cal_power(cal_type='Sp')


class CalibrateEK80(CalibrateEK):
    fs = 1.5e6  # default full sampling frequency [Hz]
    z_et = 75
    z_er = 1000

    def __init__(self, echodata):
        super().__init__(echodata)

        # cal params used by both complex and power data calibration
        # TODO: will have to add complex data-specific params, like the freq-dependent gain factor
        self.cal_params = dict.fromkeys(CAL_PARAMS['EK'])

    def get_env_params(self, env_params):
        """Get env params using user inputs or values from data file.

        EK80 file by default contains sound speed, temperature, depth, salinity, and acidity,
        therefore absorption is always calculated unless it is supplied by the user.
        In cases when temperature, salinity, and pressure values are supplied
        by the user simultaneously, both the sound speed and absorption are re-calculated.

        Parameters
        ----------
        env_params : dict
        """
        # Re-calculate environment parameters if user supply all env variables
        if ('temperature' in env_params) and ('salinity' in env_params) and ('pressure' in env_params):
            for p in ['temperature', 'salinity', 'pressure']:
                self.env_params[p] = env_params[p]
            self.env_params['sound_speed'] = uwa.calc_sound_speed(temperature=self.env_params['temperature'],
                                                                  salinity=self.env_params['salinity'],
                                                                  pressure=self.env_params['pressure'])
            self.env_params['sound_absorption'] = uwa.calc_absorption(frequency=self.echodata.raw_beam['frequency'],
                                                                      temperature=self.env_params['temperature'],
                                                                      salinity=self.env_params['salinity'],
                                                                      pressure=self.env_params['pressure'])
        # Otherwise
        #  get temperature, salinity, and pressure from raw data file
        #  get sound speed from user inputs or raw data file
        #  get absorption from user inputs or computing from env params stored in raw data file
        else:
            # pressure is encoded as "depth" in EK80  # TODO: change depth to pressure in EK80 file?
            for p1, p2 in zip(['temperature', 'salinity', 'pressure'],
                              ['temperature', 'salinity', 'depth']):
                self.env_params[p1] = env_params[p1] if p1 in env_params else self.echodata.raw_env[p2]
            self.env_params['sound_speed'] = (env_params['sound_speed']
                                              if 'sound_speed' in env_params
                                              else self.echodata.raw_env['sound_speed_indicative'])
            self.env_params['sound_absorption'] = (
                env_params['sound_absorption']
                if 'sound_absorption' in env_params
                else uwa.calc_absorption(frequency=self.echodata.raw_beam['frequency'],
                                         temperature=self.env_params['temperature'],
                                         salinity=self.env_params['salinity'],
                                         pressure=self.env_params['pressure']))

    def _get_vend_cal_params_complex(self, channel_id, filter_name, param_type):
        """Get filter coefficients stored in the Vendor group attributes.

        Parameters
        ----------
        channel_id : str
            channel id for which the param to be retrieved
        filter_name : str
            name of filter coefficients to retrieve
        param_type : str
            'coeff' or 'decimation'
        """
        if param_type == 'coeff':
            v = (self.echodata.raw_vend.attrs['%s %s filter_r' % (channel_id, filter_name)]
                 + 1j * self.echodata.raw_vend.attrs['%s %s filter_i' % (channel_id, filter_name)])
            if v.size == 1:
                v = np.expand_dims(v, axis=0)  # expand dims for convolution
            return v
        else:
            return self.echodata.raw_vend.attrs['%s %s decimation' % (channel_id, filter_name)]

    def _tapered_chirp(self, transmit_duration_nominal, slope, transmit_power, frequency_start, frequency_end):
        """Create a baseline chirp template.
        """
        t = np.arange(0, transmit_duration_nominal, 1 / self.fs)
        nwtx = (int(2 * np.floor(slope * t.size)))  # length of tapering window
        wtx_tmp = np.hanning(nwtx)  # hanning window
        nwtxh = (int(np.round(nwtx / 2)))  # half length of the hanning window
        wtx = np.concatenate([wtx_tmp[0:nwtxh],
                              np.ones((t.size - nwtx)),
                              wtx_tmp[nwtxh:]])  # assemble full tapering window
        y_tmp = (np.sqrt((transmit_power / 4) * (2 * self.z_et))  # amplitude
                 * signal.chirp(t, frequency_start, t[-1], frequency_end)
                 * wtx)  # taper and scale linear chirp
        return y_tmp / np.max(np.abs(y_tmp)), t  # amp has no actual effect

    def _filter_decimate_chirp(self, y, ch_id):
        """Filter and decimate the chirp template.

        Parameters
        ----------
        y : np.array
            chirp from _tapered_chirp
        ch_id : str
            channel_id to select the right coefficients and factors
        """
        # filter coefficients and decimation factor
        wbt_fil = self._get_vend_cal_params_complex(ch_id, 'WBT', 'coeff')
        pc_fil = self._get_vend_cal_params_complex(ch_id, 'PC', 'coeff')
        wbt_decifac = self._get_vend_cal_params_complex(ch_id, 'WBT', 'decimation')
        pc_decifac = self._get_vend_cal_params_complex(ch_id, 'PC', 'decimation')

        # WBT filter and decimation
        ytx_wbt = signal.convolve(y, wbt_fil)
        ytx_wbt_deci = ytx_wbt[0::wbt_decifac]

        # PC filter and decimation
        ytx_pc = signal.convolve(ytx_wbt_deci, pc_fil)
        ytx_pc_deci = ytx_pc[0::pc_decifac]
        ytx_pc_deci_time = np.arange(ytx_pc_deci.size) * 1 / self.fs * wbt_decifac * pc_decifac

        return ytx_pc_deci, ytx_pc_deci_time

    @staticmethod
    def _get_tau_effective(ytx, fs_deci, waveform_mode):
        """Compute effective pulse length.

        Parameters
        ----------
        ytx :array
            transmit signal
        fs_deci : float
            sampling frequency of the decimated (recorded) signal
        waveform_mode : str
            ``CW`` for CW-mode samples, either recorded as complex or power samples
            ``BB`` for BB-mode samples, recorded as complex samples
        """
        if waveform_mode == 'BB':
            ytxa = signal.convolve(ytx, np.flip(np.conj(ytx))) / np.linalg.norm(ytx) ** 2
            ptxa = abs(ytxa) ** 2
        elif waveform_mode == 'CW':
            ptxa = np.abs(ytx) ** 2  # energy of transmit signal
        else:
            raise ValueError('Input waveform_mode not recognized!')
        return ptxa.sum() / (ptxa.max() * fs_deci)  # TODO: verify fs_deci = 1.5e6 in spheroid data sets

    def get_transmit_chirp(self, waveform_mode):
        """Reconstruct transmit signal and compute effective pulse length.

        Parameters
        ----------
        waveform_mode : str
            ``CW`` for CW-mode samples, either recorded as complex or power samples
            ``BB`` for BB-mode samples, recorded as complex samples
        """
        # Make sure it is BB mode data
        if ('frequency_start' not in self.echodata.raw_beam) or ('frequency_end' not in self.echodata.raw_beam):
            raise TypeError('File does not contain BB mode complex samples!')

        y_all = {}
        y_time_all = {}
        tau_effective = {}
        for freq in self.echodata.raw_beam.frequency.values:
            # TODO: currently only deal with the case with a fixed tx key param values within a channel
            tx_param_names = ['transmit_duration_nominal', 'slope', 'transmit_power',
                              'frequency_start', 'frequency_end']
            tx_params = {}
            for p in tx_param_names:
                tx_params[p] = np.unique(self.echodata.raw_beam[p].sel(frequency=freq))
                if tx_params[p].size != 1:
                    raise TypeError('File contains changing %s!' % p)
            y_tmp, _ = self._tapered_chirp(**tx_params)

            # Filter and decimate chirp template
            channel_id = str(self.echodata.raw_beam.sel(frequency=freq)['channel_id'].values)
            fs_deci = 1 / self.echodata.raw_beam.sel(frequency=freq)['sample_interval'].values
            y_tmp, y_tmp_time = self._filter_decimate_chirp(y_tmp, channel_id)
            tau_effective_tmp = self._get_tau_effective(y_tmp, fs_deci, waveform_mode=waveform_mode)

            y_all[channel_id] = y_tmp
            y_time_all[channel_id] = y_tmp_time
            tau_effective[channel_id] = tau_effective_tmp

        return y_all, y_time_all, tau_effective

    def compress_pulse(self, chirp):
        """Perform pulse compression on the backscatter data.

        Parameters
        ----------
        chirp : np.ndarray
            transmit chirp replica indexed by channel_id
        """
        backscatter = self.echodata.raw_beam['backscatter_r'] + 1j * self.echodata.raw_beam['backscatter_i']

        pc_all = []
        for freq in self.echodata.raw_beam.frequency.values:
            backscatter_freq = (backscatter.sel(frequency=freq)
                                .dropna(dim='range_bin', how='all')
                                .dropna(dim='quadrant', how='all')
                                .dropna(dim='ping_time'))
            channel_id = str(self.echodata.raw_beam.sel(frequency=freq)['channel_id'].values)
            replica = xr.DataArray(np.conj(chirp[channel_id]), dims='window')
            # Pulse compression via rolling
            pc = (backscatter_freq.rolling(range_bin=replica.size).construct('window').dot(replica)
                  / np.linalg.norm(chirp[channel_id]) ** 2)
            # Expand dimension and add name to allow merge
            pc = pc.expand_dims(dim='frequency')
            pc.name = 'pulse_compressed_output'
            pc_all.append(pc)

        pc_merge = xr.merge(pc_all)

        return pc_merge

    def _cal_complex(self, cal_type, waveform_mode):
        """Calibrate complex data from EK80.

        Parameters
        ----------
        cal_type : str
            'Sv' for calculating volume backscattering strength, or
            'Sp' for calculating point backscattering strength
        waveform_mode : str
            ``CW`` for CW-mode samples, either recorded as complex or power samples
            ``BB`` for BB-mode samples, recorded as complex samples
        """

        # pulse compression
        chirp, _, tau_effective = self.get_transmit_chirp()
        pc = self.compress_pulse(chirp)
        prx_bb = (self.echodata.raw_beam.quadrant.size
                  * np.abs(pc.mean(dim='quadrant')) ** 2
                  / (2 * np.sqrt(2)) ** 2
                  * (np.abs(self.z_er + self.z_et) / self.z_er) ** 2
                  / self.z_et)

        # Derived params
        sound_speed = self.env_params['sound_speed'].reindex({'ping_time': self.echodata.raw_beam['ping_time']},
                                                             'nearest')
        range_meter = self.range_meter
        freq_nominal = self.echodata.raw_beam.frequency
        if waveform_mode == 'BB':
            freq_center = (self.echodata.raw_beam['frequency_start'] + self.echodata.raw_beam['frequency_end']) / 2
            wavelength = sound_speed / freq_center
        elif waveform_mode == 'CW':
            wavelength = sound_speed / freq_nominal
        else:
            raise ValueError('Input waveform_mode not recognized!')
        # gain = self.echodata.raw_vend['gain']  # TODO: need to interpolate gain to at freq_center
        gain = 27

        # Transmission loss
        spreading_loss = 20 * np.log10(range_meter.where(range_meter >= 1, other=1))
        # TODO: Check udpates of sound_absorption uses freq_center instead of freq_nominal
        absorption_loss = 2 * self.env_params['sound_absorption'].reindex(
            {'ping_time': self.echodata.raw_beam['ping_time']}, 'nearest') * range_meter

        # TODO: both Sv and Sp are off by ~<0.5 dB from matlab outputs.
        #  Is this due to the use of 'single' in matlab code?
        if cal_type == 'Sv':
            # get equivalent beam angle
            if waveform_mode == 'BB':
                psifc = self.echodata.raw_beam['equivalent_beam_angle'] + 10 * np.log10(freq_nominal / freq_center)
            elif waveform_mode == 'CW':
                psifc = self.echodata.raw_beam['equivalent_beam_angle']
            else:
                raise ValueError('Input waveform_mode not recognized!')

            # effective pulse length
            tau_effective = xr.DataArray(data=list(tau_effective.values()),
                                         coords=[self.echodata.raw_beam.frequency,
                                                 self.echodata.raw_beam.ping_time],
                                         dims=['frequency', 'ping_time'])
            out = (10 * np.log10(prx_bb)
                   + spreading_loss + absorption_loss
                   - 10 * np.log10(wavelength ** 2
                                   * self.echodata.raw_beam['transmit_power']
                                   * sound_speed
                                   / (32 * np.pi ** 2))
                   - 2 * gain - 10 * np.log10(tau_effective) - psifc)
            out = out.rename_vars({'pulse_compressed_output': 'Sv'})

        elif cal_type == 'Sp':
            out = (10 * np.log10(prx_bb)
                   + 2 * spreading_loss + absorption_loss
                   - 10 * np.log10(wavelength ** 2
                                   * self.echodata.raw_beam['transmit_power']
                                   / (16 * np.pi ** 2))
                   - 2 * gain)
            out = out.rename_vars({'pulse_compressed_output': 'Sp'})
        else:
            raise ValueError('cal_type not recognized!')

        # Attach calculated range (with units meter) into data set
        out = out.merge(range_meter)

        return out

    def get_Sv(self, mode=None):
        """Compute volume backscattering strength (Sv).

        Parameters
        ----------
        mode : str
            For EK80 data by default calibration is performed for all available data
            including complex and power samples.
            Use ``complex`` to compute Sv from only complex samples,
            and ``power`` to compute Sv from only power samples,

            For all other sonar systems, calibration for power samples is performed.

        Returns
        -------
        Sv : xr.DataSet
            A DataSet containing volume backscattering strength (``Sv``)
            and the corresponding range (``range``) in units meter.
            For EK80 data, data variable ``Sv`` contains Sv computed from power samples, and
            data variable ``Sv_complex`` contains Sv computed from complex samples.
            They are separately stored due to the dramatically different sample interval along range.
        """
        # Default to computing Sv from both power and complex samples
        flag_complex = True
        flag_power = True

        # Default to use self.echodata.raw_beam for _cal_power
        use_raw_beam_power = False

        # Figure out what cal to do
        if hasattr(self.echodata, 'raw_beam_power'):  # both power and complex samples exist
            use_raw_beam_power = True  # use self.echodata.raw_beam_power for _cal_power
            if mode == 'complex':
                flag_power = False
            elif mode == 'power':
                flag_complex = False
            else:
                raise ValueError('Input mode not recognized!')
        else:  # only power OR complex samples exist
            if 'quadrant' in self.echodata.raw_beam.dims:  # complex samples
                flag_power = False
                if mode == 'power':
                    raise TypeError('EchoData does not contain power samples!')  # user selects the wrong mode
            else:  # power samples
                flag_complex = False
                if mode == 'complex':
                    raise TypeError('EchoData does not contain complex samples!')  # user selects the wrong mode

        # Compute Sv and combine data sets if both Sv and Sv_complex are calculated
        # TODO: need to figure out what to do with the different types of range_meter in EK80 data
        if flag_power:
            self.range_meter = self.calc_range_meter(waveform_mode='CW', tvg_correction_factor=0)
            ds_Sv = self._cal_power(cal_type='Sv', use_raw_beam_power=use_raw_beam_power)
        else:
            ds_Sv = xr.Dataset()
        if flag_complex:
            # TODO: here we deal first with only BB mode encoded in complex samples.
            #  Add CW mode encoded in complex samples later.
            self.range_meter = self.calc_range_meter(waveform_mode='BB', tvg_correction_factor=0)
            ds_Sv_complex = self._cal_complex(cal_type='Sv')
        else:
            ds_Sv_complex = xr.Dataset()
        # ds_combine = xr.merge([ds_Sv, ds_Sv_complex])

        # return ds_combine
