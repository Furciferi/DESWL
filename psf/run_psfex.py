#! /usr/bin/env python
# Run PSFEx for a set of exposures, including making any necessarily input files.
# It also logs errors into a psfex blacklist file.

import os
import traceback
import astropy.io.fits as pyfits
import numpy
import copy
import glob
import time
import fitsio

# Define the parameters for the blacklist

# How many stars are too few or too many?
FEW_STARS = 20
MANY_STARS_FRAC = 0.5
# How high is a high FWHM?  3.6 arcsec / 0.26 arcsec/pixel = 13.8 pixels
HIGH_FWHM = 13.8

# flag values
NO_STARS_FLAG = 1
TOO_FEW_STARS_FLAG = 2
TOO_MANY_STARS_FLAG = 4
TOO_HIGH_FWHM_FLAG = 8
FINDSTARS_FAILURE = 16
PSFEX_FAILURE = 32
ERROR_FLAG = 64

class NoStarsException(Exception):
    pass


def parse_args():
    import argparse
    
    parser = argparse.ArgumentParser(description='Run PSFEx on a set of runs/exposures')

    # Directory arguments
    parser.add_argument('--sex_dir', default='/astro/u/rarmst/soft/bin/',
                        help='location of sextrator executable')
    parser.add_argument('--psfex_exe', default='/astro/u/rarmst/soft/bin/psfex',
                        help='location of psfex executable')
    parser.add_argument('--piff_exe', default='/astro/u/mjarvis/bin/piffify',
                        help='location of piffify executable')
    parser.add_argument('--findstars_dir', default='/astro/u/mjarvis/bin',
                        help='location wl executables')
    parser.add_argument('--work', default='./',
                        help='location of intermediate outputs')
    parser.add_argument('--tag', default=None,
                        help='A version tag to add to the directory name')
    parser.add_argument('--clear_output', default=False, action='store_const', const=True,
                        help='should the output directory be cleared before writing new files?')
    parser.add_argument('--output_dir', default=None,
                        help='location of output directory (default: $DATADIR/EXTRA/red/{run}/psfex-rerun/{exp}/)')
    parser.add_argument('--input_dir', default=None,
                        help='location of input directory (default: $DATADIR/OPS/red/{run}/red/{exp}/)')

    # Exposure inputs
    parser.add_argument('--exp_match', default='*_[0-9][0-9].fits.fz',
                        help='regexp to search for files in exp_dir')
    parser.add_argument('--file', default='',
                        help='list of run/exposures (in lieu of separate exps, runs)')
    parser.add_argument('--exps', default='', nargs='+',
                        help='list of exposures to run')
    parser.add_argument('--runs', default='', nargs='+',
                        help='list of runs')

    # Configuration files
    parser.add_argument('--sex_config',
                        default='/astro/u/mjarvis/rmjarvis/DESWL/psfex/default.sex',
                        help='sextractor config file')
    parser.add_argument('--psfex_config',
                        default='/astro/u/mjarvis/rmjarvis/DESWL/psfex/new.psfex',
                        help='psfex config file')
    parser.add_argument('--piff_config',
                        default='/astro/u/mjarvis/rmjarvis/DESWL/psfex/piff.yaml',
                        help='piff config file')
    parser.add_argument('--findstars_config',
                        default='wl.config +wl_firstcut.config',
                        help='wl config file')
    parser.add_argument('--sex_params',
                        default='/astro/u/mjarvis/rmjarvis/DESWL/psfex/sex.param_psfex',
                        help='sextractor param file')
    parser.add_argument('--sex_filter',
                        default='/astro/u/mjarvis/rmjarvis/DESWL/psfex/sex.conv',
                        help='name of sextractor filter file')
    parser.add_argument('--sex_nnw',
                        default='/astro/u/mjarvis/rmjarvis/DESWL/psfex/sex.nnw',
                        help='name of sextractor star file')
    parser.add_argument('--tapebump_file',
                        default='/astro/u/mjarvis/rmjarvis/DESWL/psfex/mask_ccdnum.txt',
                        help='name of tape bump file')
    parser.add_argument('--make_symlinks', default=0, type=int,
                        help='make symlinks in output dir, rather than move files')
    parser.add_argument('--noweight', default=False, action='store_const', const=True,
                        help='do not try to use a weight image.')


    # Options
    parser.add_argument('--rm_files', default=1, type=int,
                        help='remove unpacked files after finished')
    parser.add_argument('--blacklist', default=1, type=int,
                        help='add failed CCDs to the blacklist')
    parser.add_argument('--run_psfex', default=1, type=int,
                        help='run psfex on files')
    parser.add_argument('--run_piff', default=0, type=int,
                        help='run piff on files')
    parser.add_argument('--use_findstars', default=1, type=int,
                        help='use findstars results in psfex')
    parser.add_argument('--mag_cut', default=-1, type=float,
                        help='remove the top mags using mag_auto')
    parser.add_argument('--nbright_stars', default=10, type=int,
                        help='use median of this many brightest stars for min mag')
    parser.add_argument('--max_mag', default=-1, type=float,
                        help='only use stars brighter than this mag')
    parser.add_argument('--use_tapebumps', default=1, type=int,
                        help='avoid stars in or near tape bumps')
    parser.add_argument('--tapebump_extra', default=2, type=float,
                        help='How much extra room around tape bumps to exclude stars in units of FWHM')
    parser.add_argument('--single_ccd', default=False, action='store_const', const=True,
                        help='Only do 1 ccd per exposure (used for debugging)')
    parser.add_argument('--reserve', default=0, type=float,
                        help='Reserve some fraction of the good stars for testing')


    args = parser.parse_args()

    if args.run_piff:
        args.run_psfex = False
    return args


def read_tapebump_file(file_name):
    """Read and parse the tapebump file if we are going to need it.
    """
    raw_tbdata = numpy.genfromtxt(file_name, delimiter=',')
    # repackage this as  dict (key = ccdnum) of lists of tuples (ymin, xmin, ymax, xmax)
    tbdata = {}
    for d in raw_tbdata:
        ccdnum = int(d[0])
        if ccdnum not in tbdata:
            tbdata[ccdnum] = []
        tbdata[ccdnum].append( (int(d[1]), int(d[2]), int(d[3]), int(d[4])) )
    print 'read in tape bump file.  %d bumps for %d chips'%(len(raw_tbdata),len(tbdata))
    return tbdata

def exclude_tapebumps(tbd, data, extra):
    """
    Process the tapebump data for a particular chip.

    tbd = tbdata for a particular ccdnum,
    data = the input data for the stars
    extra = how much extra distance around the tape bumps to exclude stars in pixels
    """
    # I'm sure it doesn't matter, but add an extra 0.5 pixel to the slop because the tape bump
    # values are in terms of which pixels to include as part of the tape bump.  So the edges
    # are an extra 0.5 pixel outside of that.
    extra += 0.5
    x = data['X_IMAGE']
    y = data['Y_IMAGE']
    masks = [(y>tb[0]-extra) & (x>tb[1]-extra) & (y<tb[2]+extra) & (x<tb[3]+extra) for tb in tbd]
    mask = numpy.any(masks, axis=0)
    if sum(mask) > 0:
        print '   masking %d stars for being in or near a bump'%sum(mask)
        print '       tapebumps = ',tbd
        print '       excluded x,y = ',zip(x[mask],y[mask])
    return data[numpy.logical_not(mask)]


def log_blacklist(blacklist_file, run, exp, ccdnum, flag):
    try:
        with open(blacklist_file,'a') as f:
            f.write("%s %s %d %d\n"%(run,exp,ccdnum,flag))
    except OSError as e:
        print e
        print 'Error opening blacklist.  Wait and try again.'
        import time
        time.sleep(1)
        return log_blacklist(blacklist_file,run,exp,ccdnum,flag)


def parse_file_name(file_name):
    """Parse the file name to get the directory, the root name, and the chip number
    """
    # find out if the file is fpacked by the extension
    base_file = os.path.split(file_name)[1]

    # find the base filename
    if os.path.splitext(base_file)[1] == '.fz':
        base_file=os.path.splitext(base_file)[0]
    
    if os.path.splitext(base_file)[1] != '.fits':
        raise ValueError("Invalid file name "+file)
    root = os.path.splitext(base_file)[0]

    # Get the ccdnum.  We could do this by reading the file and looking for the 
    # CCDNUM keyword.  But it's simpler to just use the filename.  We should be
    # able to assume that the file names are not mangled.
    ccdnum = int(root.split('_')[-1])
    return root, ccdnum


def unpack_file(file_name, wdir):
    """Create the unpacked file in the work directory if necessary.

    If the unpacked file already exists, then a link is made.
    Otherwise funpack is run, outputting the result into the work directory.
    """
    print 'unpack ',file_name,' wdir = ',wdir
    # find out if the file is fpacked by the extension
    base_file = os.path.split(file_name)[1]
    print 'base_file = ',base_file

    # find the base filename
    if os.path.splitext(base_file)[1] == '.fz':
        img_file = os.path.join(wdir,os.path.splitext(base_file)[0])
        print 'img_file = ',img_file
        if os.path.lexists(img_file):
            print '   %s exists already.  Removing.'
            os.remove(img_file)
        print '   unpacking fz file'
        cmd = 'funpack -O {outf} {inf}'.format(outf=img_file, inf=file_name)
        print cmd
        for itry in range(5):
            os.system(cmd)
            if os.path.lexists(img_file): break
            print '%s was not properly made.  Retrying.'%img_file
            time.sleep(10)
    else:
        # Check that there isn't both a .fits and .fits.fz for the same image.
        # Prefer the latter if there is both.
        if os.path.lexists(file_name + '.fz.'):
            print '   Found both %s and %s.  Skipping the latter.'%(
                    base_file+'.fz.',base_file)
            return None
        # If the file is not fpacked, make a symlink into the work directory
        img_file = os.path.join(wdir,base_file)
        print 'img_file = ',img_file
        if os.path.lexists(img_file):
            print '   %s exists already.  Removing.'
            os.remove(img_file)
        print 'file_name = ',file_name
        print 'wdir = ',wdir
        os.symlink(file_name,img_file)

    if not os.path.lexists(img_file):
        print 'Unable to create %s.  Skip this file.'%img_file
        return None

    return img_file

def read_image_header(img_file):
    """Read some information from the image header.

    Currently this is just the SATURATE and FWHM values.

    Returns sat, fwhm
    """
    hdu = 0

    with pyfits.open(img_file,memmap=False) as pyf:
        sat = -1
        fwhm = 4.
        try:
            sat = pyf[hdu].header['SATURATE']
            fwhm = pyf[hdu].header['FWHM']
        except:
            print("Cannot read header information from " + img_file)
            #raise RuntimeError("Cannot read header information from " + img_file)
    return sat, fwhm
 

def run_sextractor(wdir, root, img_file, sat, fwhm, noweight,
                   sex_dir, sex_config, sex_params, sex_filter, sex_nnw):
    """Run sextractor, but only if the output file does not exist yet.
    """
    cat_file = os.path.join(wdir,root+'_psfcat.fits')

    print '   running sextractor'
    cat_cmd = "{sex_dir}/sex {img_file}[0] -c {config} -CATALOG_NAME {cat_file} -CATALOG_TYPE FITS_LDAC -PARAMETERS_NAME {params} -FILTER_NAME {filter}  -STARNNW_NAME {nnw} -DETECT_MINAREA 3".format(
        sex_dir=sex_dir, img_file=img_file, config=sex_config,
        cat_file=cat_file, params=sex_params, filter=sex_filter,
        nnw=sex_nnw, fwhm=fwhm, sat=sat)
    if not noweight:
        cat_cmd += " -WEIGHT_TYPE MAP_WEIGHT -WEIGHT_IMAGE {img_file}[2]".format(img_file=img_file)
    if fwhm != 0:
        cat_cmd += " -SEEING_FWHM {fwhm}".format(fwhm=fwhm)
    if sat != -1:
        cat_cmd += " -SATUR_LEVEL {sat}".format(sat=sat)
    print cat_cmd
    os.system(cat_cmd)
    return cat_file

def run_findstars(wdir, root, cat_file, fs_dir, fs_config):
    """Run findstars, and return a new updated catalog file to use.
    """
    star_file = wdir+'/'+root+'_findstars.fits'    

    # run find stars
    print '   running findstars'
    findstars_cmd = '{fs_dir}/findstars {fs_config} root={root} cat_ext=_psfcat.fits stars_file={star_file} input_prefix={wdir}/'.format(
            fs_dir=fs_dir, fs_config=fs_config, root=root, star_file=star_file, wdir=wdir)
    print findstars_cmd
    os.system(findstars_cmd)

    if not os.path.exists(star_file):
        print '   Error running findstars.  Rerun with verbose=2.'
        findstars_cmd = '{fs_dir}/findstars {fs_config} root={root} cat_ext=_psfcat.fits stars_file={star_file} input_prefix={wdir}/ verbose=2 debug_ext=_fs.debug'.format(
            fs_dir=fs_dir, fs_config=fs_config, root=root, star_file=star_file, 
            wdir=wdir)
        print findstars_cmd
        os.system(findstars_cmd)
        print '   The debug file is',root + '_fs.debug'
        if not os.path.exists(star_file):
            return None, None, None

    # Make a mask based on which objects findstars decided are stars.
    with pyfits.open(star_file,memmap=False) as pyf:
        mask = pyf[1].data['star_flag']==1
    nstars = numpy.count_nonzero(mask)
    ntot = len(mask)
    print '   found %d stars'%nstars
    if nstars == 0:
        # Can't really do any of the rest of this, so skip out to the end.
        raise NoStarsException()

    # Read the information from the initial catalog file, including the bogus first two hdus.
    with pyfits.open(cat_file,memmap=False) as pyf:
        # Need to make copy of these to not fail
        hdu1 = copy.copy(pyf[0])
        hdu2 = copy.copy(pyf[1])
        data = pyf[2].data[mask]

        # create new catalog file with only these entries
        hdu3 = pyfits.BinTableHDU(data)
        hdu3.name = 'LDAC_OBJECTS'
        hdu_list = pyfits.HDUList([hdu1, hdu2, hdu3])
        new_cat_file = cat_file.replace('psfcat','psfcat_findstars')
        hdu_list.writeto(new_cat_file,clobber=True)

    return new_cat_file, nstars, ntot

def remove_bad_stars(wdir, root, ccdnum, cat_file, tbdata,
                     mag_cut, nbright_stars, max_mag,
                     use_tapebumps, tapebump_extra, reserve, fwhm):
    """Remove stars that are considered bad for some reason.

    Currently these reasons include:
    - Magnitude indicates that the star is significantly contaminated by the brighter/fatter
      effect.
    - Star falls in or near the tape bumps.
    """

    # get the brightest 10 stars that have flags=0 and take the median just in case some
    # strange magnitudes were selected
    with pyfits.open(cat_file,memmap=False) as pyf:
        data = copy.copy(pyf[2].data)

    # Start with a basic FLAGS==0 mask:
    flags_mask = data['FLAGS']==0
    print '   nstars with FLAGS==0 = ',numpy.count_nonzero(flags_mask)
    data = data[flags_mask]

    # Start with the current name.  We will update it below.
    new_cat_file = cat_file

    if mag_cut > 0:
        mags = numpy.sort(data['MAG_AUTO'])
        min_star = numpy.median(mags[0:nbright_stars])
        print '   min mag = ',mags[0]
        print '   median of brightest %d is '%nbright_stars, min_star

        mag_mask = data['MAG_AUTO'] > (min_star+mag_cut)
        print '   select stars dimmer than',min_star+mag_cut
        print '   which includes %d stars'%numpy.count_nonzero(mag_mask)

        data = data[mag_mask]
        print '   after exclude bright: len(data) = ',len(data)
        new_cat_file = new_cat_file.replace('psfcat','psfcat_magcut_%0.1f'%mag_cut)

    if max_mag > 0:
        mag_mask = (data['MAG_AUTO'] < max_mag)
        print '   also select stars brighter than',max_mag
        print '   which now includes %d stars'%numpy.count_nonzero(mag_mask)

        data = data[mag_mask]
        print '   after exclude faint: len(data) = ',len(data)
        new_cat_file = new_cat_file.replace('psfcat','psfcat_maxmag_%0.1f'%max_mag)

    if use_tapebumps:
        data = exclude_tapebumps(tbdata[ccdnum], data, tapebump_extra * fwhm)
        print '   after exclude tapebumps: len(data) = ',len(data)
        new_cat_file = new_cat_file.replace('psfcat','psfcat_tb')

    if reserve:
        print '   reserve ',reserve
        n = len(data)
        perm = numpy.random.permutation(n)
        n1 = int(reserve * n)
        print '   initial ids = ',data['NUMBER']
        reserve_data = data[perm[:n1]]
        data = data[perm[n1:]]
        print '   reserve_data ids = ',reserve_data['NUMBER']
        print '   final ids = ',data['NUMBER']
        print '   after reserve: len(data) = ',len(data)
        new_cat_file = new_cat_file.replace('psfcat','psfcat_reserve_%0.2f'%reserve)

        reserve_file = os.path.join(wdir,root+'_reserve.fits')
        cols = []
        dtypes = []
        for name in ['NUMBER', 'FLAGS', 'XWIN_IMAGE', 'YWIN_IMAGE', 'BACKGROUND', 
                     'ALPHAWIN_J2000', 'DELTAWIN_J2000', 'FLUX_RADIUS']:
            cols.append(reserve_data[name])
            dtypes.append((name, reserve_data[name].dtype))
        print 'cols = ',cols
        print 'dtypes = ',dtypes
        reserve_data = numpy.array(list(zip(*cols)), dtype=dtypes)
        with fitsio.FITS(reserve_file,'rw',clobber=True) as f:
            f.write_table(reserve_data)

    # create new catalog file with only these entries
    with pyfits.open(cat_file,memmap=False) as pyf:
        hdu1 = copy.copy(pyf[0])
        hdu2 = copy.copy(pyf[1])
        hdu3 = pyfits.BinTableHDU(data)
        hdu3.name = 'LDAC_OBJECTS'
        hdu_list = pyfits.HDUList([hdu1, hdu2, hdu3])
        # Apparently pyf still needs to be open when this command occurs in order to 
        # be able to handle the hdu1 and hdu2 objects correctly.
        # Hence, we don't read those earlier when we read data.
        hdu_list.writeto(new_cat_file,clobber=True)

    return new_cat_file, len(data)


def get_fwhm(cat_file):
    """Get the fwhm from the SExtractor FLUX_RADIUS estimates.
    """

    # get the brightest 10 stars that have flags=0 and take the median just in case some
    # strange magnitudes were selected
    with pyfits.open(cat_file,memmap=False) as pyf:
        data = pyf[2].data
        fwhm = 2. * data['FLUX_RADIUS']  # 2 * flux_radius is approx fwhm
    stats = ( numpy.min(fwhm), numpy.max(fwhm),
              numpy.mean(fwhm), numpy.median(fwhm) )
    #print 'FLUX_RADIUS min, max, mean, median = ',stats
    return stats


def run_psfex(wdir, root, cat_file, psf_file, used_file, xml_file, resid_file,
              psfex_exe, psfex_config):
    """Run PSFEx

    Returns True if successful, False if there was a catastrophic failure and no output 
    file was written.
    """
    if not os.path.exists(psfex_config) and '/' not in psfex_config:
        psfex_config = os.path.join('/astro/u/mjarvis/rmjarvis/DESWL/psfex/',psfex_config)
    if os.path.lexists(psf_file):
        print '   deleting existing',psf_file
        os.unlink(psf_file)
    print '   running psfex'
    psf_cmd = '{psfex_exe} {cat_file} -c {config} -OUTCAT_TYPE FITS_LDAC -OUTCAT_NAME {used_file} -XML_NAME {xml_file}'.format(
            psfex_exe=psfex_exe, cat_file=cat_file, config=psfex_config,
            used_file=used_file, xml_file=xml_file)
    print psf_cmd
    os.system(psf_cmd)

    # PSFEx generates its output filename from the input catalog name.  If this doesn't match
    # our target name, then rename it.
    actual_psf_file = cat_file.replace('.fits','.psf')

    if not os.path.exists(actual_psf_file):
        print '   Error running PSFEx.  No ouput file was written.'
        return False

    if psf_file != actual_psf_file:
        os.rename(actual_psf_file, psf_file)
    return True


def run_piff(wdir, root, img_file, cat_file, psf_file, piff_exe, piff_config):
    """Run Piffify

    Returns True if successful, False if there was a catastrophic failure and no output 
    file was written.
    """
    if not os.path.exists(piff_config) and '/' not in piff_config:
        piff_config = os.path.join('/astro/u/mjarvis/rmjarvis/DESWL/psfex/',piff_config)
    if os.path.lexists(psf_file):
        print '   deleting existing',psf_file
        os.unlink(psf_file)
    print '   running piff'
    psf_cmd = '{piff_exe} {config} input.images={images} input.cats={cat_file} output.dir={wdir} output.file_name={psf_file}'.format(
            piff_exe=piff_exe, config=piff_config, images=img_file, cat_file=cat_file,
            wdir=wdir, psf_file=psf_file)
    print psf_cmd
    os.system(psf_cmd)

    if not os.path.exists(psf_file):
        print '   Error running Piff.  No ouput file was written.'
        return False

    return True

def remove_temp_files(wdir, root, *args):
    """Remove wdir/root* except for any files listed in the args
    """
    files = sorted(glob.glob('%s/%s*'%(wdir,root)))
    for save in args:
        if save in files:
            files.remove(save)
        else:
            print 'WARNING: %s not found in %s'%(save,wdir)

    print '   Removing the following files from ',wdir
    for f in files:
        print '       ',os.path.split(f)[1]
        os.remove(f)
    print '   Done'


def move_files(wdir, odir, *args, **kwargs):
    """Either move files from wdir to odir or make symlinks.
    """
    make_symlinks = kwargs.pop('make_symlinks',False)
    for file in args:
        print 'file = ',file
        # The file might not exist if psfex had an error.
        if os.path.exists(file):
            new_file = os.path.join(odir,os.path.basename(file))
            print 'new_file = ',new_file
            if file == new_file:
                continue
            try:
                if os.path.lexists(new_file):
                    os.unlink(new_file)
            except OSError as e:
                print "Ignore OSError from remove(new_file):"
                print e
                pass
            if make_symlinks:
                print '   make link: ',new_file,' to ',file
                os.symlink(file,new_file)
            elif file != new_file:
                print '   move ',file,' to ',new_file
                os.rename(file,new_file)
                # Also make a symlink in the work directory to the new file location.
                os.symlink(new_file,file)

def main():
    args = parse_args()
    if args.use_tapebumps:
        tbdata = read_tapebump_file(args.tapebump_file)
    blacklist_file = '/astro/u/astrodat/data/DES/EXTRA/blacklists/psfex'
    if args.tag:
        blacklist_file += '-' + args.tag
    blacklist_file += '.txt'

    # Make the work directory if it does not exist yet.
    work = os.path.expanduser(args.work)
    print 'work dir = ',work
    try:
        if not os.path.exists(work):
            os.makedirs(work)
    except OSError as e:
        print "Ignore OSError from makedirs(work):"
        print e
        pass

    datadir = '/astro/u/astrodat/data/DES'

    if args.file != '':
        print 'Read file ',args.file
        with open(args.file) as fin:
            data = [ line.split() for line in fin ]
        runs, exps = zip(*data)
    else:
        runs = args.runs
        exps = args.exps

    print 'runs = ',runs
    print 'exps = ',exps

    for run,exp in zip(runs,exps):

        print 'Start work on run, exp = ',run,exp

        # Make the output directory for this exposure and clear it if necessary.
        if args.output_dir is None:
            if args.tag:
                tag_str = args.tag + "/"
            else:
                tag_str = ""
            odir = os.path.join(datadir,'EXTRA/red/%s/psfex-rerun/%s%s/'%(run,tag_str,exp))
        else:
            odir = args.output_dir
        if args.clear_output:
            if os.path.exists(odir):
                for f in os.listdir(odir):
                    try:
                        os.remove(os.path.join(odir, f))
                    except OSError as e:
                        print "Ignore OSError from remove(odir/f):"
                        print e
                        pass
        try:
            os.makedirs(odir)
        except:
            if not os.path.exists(odir): raise
        print 'odir = ',odir

        # Make the work directory for this exposure and clear it if necessary.
        wdir = os.path.join(work,exp)
        if args.clear_output:
            import shutil
            if os.path.exists(wdir):
                for f in os.listdir(wdir):
                    try:
                        os.remove(os.path.join(wdir, f))
                    except OSError as e:
                        print "Ignore OSError from remove(wdir/f):"
                        print e
                        pass
        try:
            os.makedirs(wdir)
        except:
            if not os.path.exists(wdir): raise
        print 'wdir = ',wdir

        # The input directory from the main DESDM reduction location.
        if args.input_dir is None:
            input_dir = os.path.join(datadir,'OPS/red/%s/red/%s/'%(run,exp))
        else:
            input_dir = args.input_dir
        print 'input_dir = ',input_dir

        # Get the file names in that directory.
        print '%s/%s'%(input_dir,args.exp_match)
        files = sorted(glob.glob('%s/%s'%(input_dir,args.exp_match)))

        for file_name in files:
            print '\nProcessing ', file_name
            flag = 0

            try:
                root, ccdnum = parse_file_name(file_name)
            except:
                if args.use_tapebumps:
                    print '   Unable to parse file_name %s.  Skipping this file.'%file_name
                    continue
                else:
                    base_file = os.path.split(file_name)[1]
                    if os.path.splitext(base_file)[1] == '.fz':
                        base_file=os.path.splitext(base_file)[0]
                    root = os.path.splitext(base_file)[0]
                    ccdnum = 0
            print '   root, ccdnum = ',root,ccdnum
            cat_file = None

            try:

                if args.run_psfex or args.run_piff or args.use_findstars or args.mag_cut>0 or args.use_tapebumps:
                    # Unpack the image file if necessary
                    img_file = unpack_file(file_name, wdir)
                    if img_file is None:
                        # This was our signal to skip this without blacklisting.  Just continue.
                        continue

                    # extract the saturation level, this is how desdm runs sextractor
                    # we need the fwhm for class star
                    # Also need the fwhm for doing the tape bumps.
                    sat, fwhm = read_image_header(img_file)
                    print '   fwhm = ',fwhm

                    cat_file = run_sextractor(wdir, root, img_file, sat, fwhm, args.noweight,
                                              args.sex_dir, args.sex_config, args.sex_params, 
                                              args.sex_filter, args.sex_nnw)

                # if we want to use only the stars selected by findstars
                if args.use_findstars:
                    tmp = cat_file
                    cat_file, nstars, ntot = run_findstars(
                            wdir, root, cat_file, args.findstars_dir, args.findstars_config)
                    if cat_file == None:
                        print '     -- flag for findstars failure'
                        flag |= FINDSTARS_FAILURE
                        raise NoStarsException()
                    # Check if there are few or many staras.
                    if nstars < FEW_STARS:
                        print '     -- flag for too few stars: ',nstars
                        flag |= TOO_FEW_STARS_FLAG
                    if nstars > MANY_STARS_FRAC * ntot:
                        print '     -- flag for too many stars: %d/%d'%(nstars,ntot)
                        flag |= TOO_MANY_STARS_FLAG


                # If we want to cut the brighest magnitudes
                if args.mag_cut>0 or args.use_tapebumps or args.max_mag>0 or args.reserve>0:
                    cat_file, nstars = remove_bad_stars(
                            wdir, root, ccdnum, cat_file, tbdata,
                            args.mag_cut, args.nbright_stars, args.max_mag,
                            args.use_tapebumps, args.tapebump_extra, args.reserve, fwhm)
                    # Recheck this.
                    if nstars < FEW_STARS:
                        print '     -- flag for too few stars: ',nstars
                        flag |= TOO_FEW_STARS_FLAG
                    if nstars <= 1:
                        raise NoStarsException()

                if args.run_psfex or args.run_piff or args.use_findstars or args.mag_cut>0 or args.use_tapebumps:
                    # Get the median fwhm of the given stars
                    star_fwhm = get_fwhm(cat_file)
                    print '   fwhm of stars = ',star_fwhm
                    print '   cf. header fwhm = ',fwhm
                    if star_fwhm[3] > HIGH_FWHM:
                        print '     -- flag for too high fwhm'
                        flag |= TOO_HIGH_FWHM_FLAG
                    if star_fwhm[3] > 1.5 * fwhm:
                        print '     -- flag for too high fwhm compared to fwhm from fits header'
                        flag |= TOO_HIGH_FWHM_FLAG
    
                if args.use_findstars:
                    star_file = os.path.join(wdir,root+'_findstars.fits')
                else:
                    star_file = cat_file
                psf_file = os.path.join(wdir,root+'_psfcat.psf')
                used_file = os.path.join(wdir,root+'_psfcat.used.fits')
                reserve_file = os.path.join(wdir,root+'_reserve.fits')
                xml_file = os.path.join(wdir,root+'_psfcat.xml')

                if args.run_psfex:
                    # PSFEx does this weird thing where it takes the names of the resid file,
                    # strips off the .fits ending, and replaces it with _ + cat_file
                    resid_file1 = os.path.join(wdir,'resid.fits')
                    print 'resid_file1 = ',resid_file1
                    cat_fname = os.path.basename(cat_file)
                    print 'cat_fname = ',cat_fname
                    resid_file2 = os.path.join(wdir,'resid_'+cat_fname)
                    print 'resid_file2 = ',resid_file2
                    success = run_psfex(wdir, root, cat_file, psf_file, used_file, xml_file,
                                        resid_file1, args.psfex_exe, args.psfex_config)
                    if success:
                        move_files(wdir, odir, psf_file,
                                   make_symlinks=args.make_symlinks)
                    else:
                        flag |= PSFEX_FAILURE

                if args.run_piff:
                    cat_fname = os.path.basename(cat_file)
                    print 'cat_fname = ',cat_fname
                    resid_file2 = os.path.join(wdir,'resid_'+cat_fname)
                    print 'resid_file2 = ',resid_file2
                    success = run_piff(wdir, root, img_file, cat_file, psf_file,
                                       args.piff_exe, args.piff_config)
                    if success:
                        move_files(wdir, odir, psf_file,
                                   make_symlinks=args.make_symlinks)
                    else:
                        flag |= PSFEX_FAILURE

                print 'rm_files = ',args.rm_files
                if args.rm_files:
                    remove_temp_files(wdir, root, star_file, psf_file, used_file, reserve_file,
                                      xml_file)

            except NoStarsException:
                print 'No stars.  Log this in the blacklist and continue.'
                flag |= NO_STARS_FLAG
            except Exception as e:
                print 'Caught exception: ',e
                traceback.print_exc()
                print 'Log this in the blacklist and continue.'
                flag |= ERROR_FLAG

            if flag and args.blacklist:
                log_blacklist(blacklist_file,run,exp,ccdnum,flag)

            if args.single_ccd:
                break

    print '\nFinished processing all exposures'


if __name__ == "__main__":
    main()
