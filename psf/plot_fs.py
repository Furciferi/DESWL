#! /usr/bin/env python
# Compute rho statistics on PSFEx outputs.
# This involves creating catalogs of shapes based on the PSFEx files, and then using
# TreeCorr to compute the correlation functions.

import os
import matplotlib
matplotlib.use('Agg') # needs to be done before import pyplot
import matplotlib.pyplot as plt
plt.style.use('/astro/u/mjarvis/.config/matplotlib/stylelib/supermongo.mplstyle')

# Overall zeropoint adjustment from Eli.
zp = 5.3

FINDSTARS_FAILURE = 16

def parse_args():
    import argparse
    
    parser = argparse.ArgumentParser(description='Run PSFEx on a set of runs/exposures')

    # Drectory arguments
    parser.add_argument('--work', default='./',
                        help='location of work directory')
    parser.add_argument('--tag', default=None,
                        help='A version tag to add to the directory name')

    # Exposure inputs
    parser.add_argument('--exp_match', default='*_[0-9][0-9].fits.fz',
                        help='regexp to search for files in exp_dir')
    parser.add_argument('--file', default='',
                        help='list of run/exposures (in lieu of separate exps, runs)')
    parser.add_argument('--exps', default='', nargs='+',
                        help='list of exposures to run')

    # Options
    parser.add_argument('--do_blacklist', default=False, action='store_const', const=True,
                        help='Blacklist files that have bad findstars properties')
    parser.add_argument('--do_plot', default=None, action='store_const', const=True,
                        help='Do plot even if blacklisting')
    parser.add_argument('--no_plot', default=None, action='store_const', const=True,
                        help='Do not do plot')
    parser.add_argument('--make_stats', default=False, action='store_const', const=True,
                        help='Make stats.fits with statistics about the sizes')
    parser.add_argument('--single_ccd', default=False, action='store_const', const=True,
                        help='Only do 1 ccd per exposure (used for debugging)')

    args = parser.parse_args()

    # Default is to not plot when blacklisting.
    if args.do_plot is None:
        if args.do_blacklist:
            args.do_plot = False
        else:
            args.do_plot = True
    if args.no_plot:
        args.do_plot = False

    return args

def log_blacklist(blacklist_file, exp, ccdnum, flag):
    try:
        with open(blacklist_file,'a') as f:
            f.write("%s %s %d %d\n"%(exp,ccdnum,flag))
    except OSError as e:
        print e
        print 'Error opening blacklist.  Wait and try again.'
        import time
        time.sleep(1)
        return log_blacklist(blacklist_file,exp,ccdnum,flag)


def plot_fs(size, mag, flag, filename, modest=None):
    import numpy
    fig, ax = plt.subplots(1,1)

    # Mag adjustment from Eli.
    mag = mag + zp

    ax.set_xlim(10+zp, 24.)
    ax.set_ylim(0., 1.2)
    ax.set_xlabel('Magnitude')
    ax.set_ylabel(r'$T = 2\sigma^2$ (arcsec${}^2$)')

    index = numpy.argsort(mag)
    n = numpy.arange(len(mag))

    for i in range(20):
        s = index[n%20==i]

        if modest is not None:
            mod = ax.scatter(mag[s][modest[s]&(flag[s]<4)], size[s][modest[s]&(flag[s]<4)],
                             facecolors='none', edgecolors='blue', s=50., lw=0.8, alpha=0.3)

        det = ax.scatter(mag[s][flag[s] == 0], size[s][flag[s] == 0],
                         color='black', marker='o', s=2.)
        cand = ax.scatter(mag[s][flag[s] == 1], size[s][flag[s] == 1],
                          color='magenta', marker='o', s=8.)
        psf = ax.scatter(mag[s][flag[s] == 2], size[s][flag[s] == 2],
                         color='green', marker='*', s=40.)

        legend_items = [det, cand, psf]
        if modest is not None:
            legend_items += [mod]

    ax.legend(legend_items,
              ['Detected Object', 'Candidate Star', 'PSF Star', 'Modest Star'],
              loc='upper left', frameon=True)

    plt.tight_layout()
    plt.savefig(filename)
    plt.close(fig)

    numpy.savetxt(os.path.splitext(filename)[0] + '.dat',
                  numpy.array(zip(size, mag, flag), dtype='f8, f8, i2'), fmt='%r',
                  header='size  mag  flag (0=detected, 1=candidate, 2=psf, 4=bad measurement)')


def main():
    import os
    import glob
    import galsim
    import json
    import numpy
    import astropy.io.fits as pyfits

    args = parse_args()

    blacklist_file = '/astro/u/astrodat/data/DES/EXTRA/blacklists/psfex'
    if args.tag:
        blacklist_file += '-' + args.tag
    blacklist_file += '.txt'

    # Make the work directory if it does not exist yet.
    work = os.path.expanduser(args.work)
    print 'work dir = ',work
    try:
        if not os.path.isdir(work):
            os.makedirs(work)
    except OSError as e:
        print "Ignore OSError from makedirs(work):"
        print e
        pass

    datadir = '/astro/u/astrodat/data/DES'

    # A listing Erin made of all the exposures in Y3 used in meds files
    all_exp = fitsio.read('/astro/u/mjarvis/work/y3_piff/exposures-ccds-Y3A1_COADD.fits')

    if args.file != '':
        print('Read file ',args.file)
        with open(args.file) as fin:
            exps = [ line for line in fin if line[0] != '#' ]
        print('File includes %d exposures'%len(exps))
    elif args.exps is not None:
        exps = args.exps
        print('Explicit listing of %d exposures'%len(exps))
    else:
        exps = sorted(list(set(all_exp['expnum'])))
        print('There are a total of %d exposures'%len(exps))

    cat_dir = os.path.join(work,'psf_cats')

    stats = []

    for exp in exps:
        print 'Start work on exp = ',exp
        exp = int(exp)
        data = all_exp[all_exp['expnum'] == exp]

        wdir = os.path.join(work,str(exp))
        print('wdir = ',wdir)

        for row in data:
            key, expnum, ccdnum, band = row['key'], row['expnum'], row['ccdnum'], row['band']
            print('\nProcessing ', key, expnum, ccdnum, band)
            path = row['path'].strip()
            print('path = ',path)
            flag = 0

            try:

                image_file_name = path.rsplit('/')[1]
                root = image_file_name.rsplit('_immasked')[0]
                star_file_name = root + '_starcat.fits'
                star_file = os.path.join(wdir,star_file_name)
                print('star_file = ',star_file_name)

                star_data = fitsio.read(star_file)

                mag = star_data['MAG_AUTO']
                size = 2. * cat_data['sigma0']**2
                # Flag definitions:
                # 0 = good detection
                # 1 = star candidate
                # 2 = final star
                # 4 = bad measurement
                flag = cat_data['star_flag']
                print 'ntot = ',len(cat_data)
                print 'nstar = ',numpy.sum(flag==1)
                cand = numpy.where(flag == 1)[0]
                flag[cat_data['size_flags']>0] = 4
                print 'nbad = ',numpy.sum(flag==4)

                x = cat_data['x']
                y = cat_data['y']

                psfex_file = os.path.join(exp_dir, exp + "_%02d_psfcat.used.fits"%ccdnum)
                with pyfits.open(psfex_file) as pyf:
                    psfex_data = pyf[2].data

                xu = psfex_data['X_IMAGE']
                yu = psfex_data['Y_IMAGE']
                used = [ numpy.where((numpy.abs(xxu-x)<1) & (numpy.abs(yyu-y)<1))[0][0] for (xxu,yyu) in zip(xu,yu) ]
                flag[used] = 2
                print 'nused = ',numpy.sum(flag==2)

                xs = sex_data['X_IMAGE']
                ys = sex_data['Y_IMAGE']
                # Matching this is a bit tricky, since they are different detections, measurements
                # Just pick out the items with a single match within 1 pixel.
                m1 = [ numpy.where((numpy.abs(xxs-x)<1) & (numpy.abs(yys-y)<1))[0] for (xxs,yys) in zip(xs,ys) ]
                m2 = numpy.array([ len(a) for a in m1 ])
                cat_m = numpy.concatenate(numpy.array(m1)[m2==1])
                # Now cat_data[cat_m] gives rows that are in the original sextractor catalog.
                sex_m = numpy.where(m2==1)
                # And sex_data[sex_m] gives the corresponding rows in the sextractor catalog.
                modest = numpy.zeros_like(size, dtype=bool)
                bright_test = (sex_data[sex_m]['CLASS_STAR'] > 0.3) & (sex_data[sex_m]['MAG_AUTO'] < 18.0-zp)
                locus_test = (sex_data[sex_m]['SPREAD_MODEL'] + 3.*sex_data[sex_m]['SPREADERR_MODEL'] < 0.003)
                faint_psf_test = (sex_data[sex_m]['MAG_PSF'] > 30.-zp) & (sex_data[sex_m]['MAG_AUTO'] < 21.-zp)
                modest[cat_m] = (bright_test | locus_test) & ~faint_psf_test

                nused_modest = numpy.sum( (flag==2) & modest )
                nused_not_modest = numpy.sum( (flag==2) & ~modest )

                # These two get named, since we might use them to do the blacklist.
                meanT = numpy.mean(size[cand])
                stdT = numpy.std(size[cand])

            if args.make_stats:
                stats.append( (expnum, ccdnum,
                               len(cand), len(used),
                               nused_modest, nused_not_modest,
                               numpy.min(size[cand]), numpy.max(size[cand]),
                               meanT, stdT,
                               numpy.median(size[cand]), 
                               numpy.min(size[used]), numpy.max(size[used]),
                               numpy.mean(size[used]), numpy.std(size[used]),
                               numpy.median(size[used]), 
                               ) )

            if args.do_plot:
                plot_file = os.path.join(exp_dir, exp + "_%02d.pdf"%ccdnum)
                #plot_fs(size, mag, flag, plot_file, modest)
                plot_fs(size, mag, flag, plot_file)

            if args.do_blacklist and stdT > 0.15 * meanT:
                log_blacklist(blacklist_file,run,exp,ccdnum,flag=FINDSTARS_FAILURE)

            if args.single_ccd:
                break
          except Exception as e:  # In case any errors
            print 'Caught exception: ',e
            continue
      except Exception as e:  # In case any errors
        print 'Caught exception: ',e
        continue

    if args.make_stats:
        dt = numpy.dtype([
            ('expnum', 'i8'),
            ('ccdnum', 'i8'),
            ('ncand', 'i8'),
            ('nused', 'i8'),
            ('nused_modest', 'i8'),
            ('nused_not_modest', 'i8'),
            ('min_cand', 'f4'),
            ('max_cand', 'f4'),
            ('mean_cand', 'f4'),
            ('std_cand', 'f4'),
            ('median_cand', 'f4'),
            ('min_used', 'f4'),
            ('max_used', 'f4'),
            ('mean_used', 'f4'),
            ('std_used', 'f4'),
            ('median_used', 'f4'),
            ])

        recstats = numpy.array(stats, dtype=dt)
        import fitsio
        print 'Median ncand = ',numpy.median(recstats['ncand'])
        print 'Median nused = ',numpy.median(recstats['nused'])
        print 'Median seeing = ',numpy.median(recstats['median_used'])

        fitsio.write('stats.fits', recstats, clobber=True)

    print '\nFinished processing all exposures'


if __name__ == "__main__":
    main()
