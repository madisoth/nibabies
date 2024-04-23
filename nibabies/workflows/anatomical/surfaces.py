"""Anatomical surface projections"""

import typing as ty

import templateflow.api as tf
from nipype.interfaces import freesurfer as fs
from nipype.interfaces import io as nio
from nipype.interfaces import utility as niu
from nipype.pipeline import engine as pe
from niworkflows.engine.workflows import LiterateWorkflow
from niworkflows.interfaces.freesurfer import (
    PatchedLTAConvert as LTAConvert,
)
from niworkflows.interfaces.freesurfer import (
    PatchedRobustRegister as RobustRegister,
)
from niworkflows.interfaces.patches import FreeSurferSource
from smriprep.interfaces.freesurfer import MakeMidthickness
from smriprep.interfaces.workbench import SurfaceResample
from smriprep.workflows.surfaces import _extract_fs_fields

SURFACE_INPUTS = [
    't1w',
    't2w',
    'flair',
    'skullstripped_t1',
    'subjects_dir',
    'subject_id',
    # Customize aseg
    'in_aseg',
    'in_mask',
]
SURFACE_OUTPUTS = [
    'subjects_dir',
    'subject_id',
    'anat2fsnative_xfm',
    'fsnative2anat_xfm',
]


def init_mcribs_surface_recon_wf(
    *,
    omp_nthreads: int,
    use_aseg: bool,
    use_mask: bool,
    precomputed: dict,
    mcribs_dir: str | None = None,
    name: str = 'mcribs_surface_recon_wf',
):
    """
    Reconstruct cortical surfaces using the M-CRIB-S pipeline.

    This workflow injects a precomputed segmentation into the M-CRIB-S pipeline, bypassing the
    DrawEM segmentation step that is normally performed.
    """
    from niworkflows.interfaces.nibabel import MapLabels, ReorientImage

    from nibabies.interfaces.mcribs import MCRIBReconAll

    if not use_aseg:
        raise NotImplementedError(
            'A previously computed segmentation is required for the M-CRIB-S workflow.'
        )

    inputnode = pe.Node(niu.IdentityInterface(fields=SURFACE_INPUTS), name='inputnode')
    outputnode = pe.Node(niu.IdentityInterface(fields=SURFACE_OUTPUTS), name='outputnode')

    workflow = LiterateWorkflow(name=name)
    workflow.__desc__ = (
        'Brain surfaces were reconstructed with a modified `MCRIBReconAll` [M-CRIB-S, @mcribs]'
        'workflow, using the reference T2w and a pre-computed anatomical segmentation'
    )

    # mapping of labels from FS to M-CRIB-S
    fs2mcribs = {
        2: 51,
        3: 21,
        4: 49,
        5: 0,
        7: 17,
        8: 17,
        10: 43,
        11: 41,
        12: 47,
        13: 47,
        14: 0,
        15: 0,
        16: 19,
        17: 1,
        18: 3,
        26: 41,
        28: 45,
        31: 49,
        41: 52,
        42: 20,
        43: 50,
        44: 0,
        46: 18,
        47: 18,
        49: 42,
        50: 40,
        51: 46,
        52: 46,
        53: 2,
        54: 4,
        58: 40,
        60: 44,
        63: 50,
        253: 48,
    }
    map_labels = pe.Node(MapLabels(mappings=fs2mcribs), name='map_labels')

    t2w_las = pe.Node(ReorientImage(target_orientation='LAS'), name='t2w_las')
    seg_las = t2w_las.clone(name='seg_las')

    mcribs_recon = pe.Node(
        MCRIBReconAll(
            surfrecon=True,
            surfrecon_method='Deformable',
            join_thresh=1.0,
            fast_collision=True,
            nthreads=omp_nthreads,
        ),
        name='mcribs_recon',
        mem_gb=5,
    )
    if mcribs_dir:
        mcribs_recon.inputs.outdir = mcribs_dir
        mcribs_recon.config = {'execution': {'remove_unnecessary_outputs': False}}

    if use_mask:
        # If available, dilated mask and use in recon-neonatal-cortex
        from niworkflows.interfaces.morphology import BinaryDilation

        mask_dil = pe.Node(BinaryDilation(radius=3), name='mask_dil')
        mask_las = t2w_las.clone(name='mask_las')
        workflow.connect([
            (inputnode, mask_dil, [('in_mask', 'in_mask')]),
            (mask_dil, mask_las, [('out_mask', 'in_file')]),
            (mask_las, mcribs_recon, [('out_file', 'mask_file')]),
        ])  # fmt:skip

    mcribs_postrecon = pe.Node(
        MCRIBReconAll(autorecon_after_surf=True, nthreads=omp_nthreads),
        name='mcribs_postrecon',
        mem_gb=5,
    )

    fssource = pe.Node(FreeSurferSource(), name='fssource', run_without_submitting=True)
    midthickness_wf = init_midthickness_wf(omp_nthreads=omp_nthreads)

    workflow.connect([
        (inputnode, t2w_las, [('t2w', 'in_file')]),
        (inputnode, map_labels, [('in_aseg', 'in_file')]),
        (map_labels, seg_las, [('out_file', 'in_file')]),
        (inputnode, mcribs_recon, [
            ('subjects_dir', 'subjects_dir'),
            ('subject_id', 'subject_id')]),
        (t2w_las, mcribs_recon, [('out_file', 't2w_file')]),
        (seg_las, mcribs_recon, [('out_file', 'segmentation_file')]),
        (inputnode, mcribs_postrecon, [
            ('subjects_dir', 'subjects_dir'),
            ('subject_id', 'subject_id')]),
        (mcribs_recon, mcribs_postrecon, [('mcribs_dir', 'outdir')]),
        (mcribs_postrecon, fssource, [('subjects_dir', 'subjects_dir')]),
        (inputnode, fssource, [('subject_id', 'inputnode.subject_id')]),
        (fssource, midthickness_wf, [
            ('white', 'inputnode.white'),
            ('graymid', 'inputnode.graymid'),
        ]),
        (midthickness_wf, outputnode, [
            ('outputnode.subjects_dir', 'subjects_dir'),
            ('outputnode.subject_id', 'subject_id'),
        ]),
    ])  # fmt:skip

    if 'fsnative' not in precomputed.get('transforms', {}):
        fsnative2anat_xfm = pe.Node(
            RobustRegister(auto_sens=True, est_int_scale=True),
            name='fsnative2anat_xfm',
        )
        anat2fsnative_xfm = pe.Node(
            LTAConvert(out_lta=True, invert=True),
            name='anat2fsnative_xfm',
        )
        workflow.connect([
            (inputnode, fsnative2anat_xfm, [('t2w', 'target_file')]),
            (fssource, fsnative2anat_xfm, [('T2', 'source_file')]),
            (fsnative2anat_xfm, outputnode, [('out_reg_file', 'fsnative2anat_xfm')]),
            (fsnative2anat_xfm, anat2fsnative_xfm, [('out_reg_file', 'in_lta')]),
            (anat2fsnative_xfm, outputnode, [('out_lta', 'anat2fsnative_xfm')]),
        ])  # fmt:skip

    return workflow


def init_mcribs_dhcp_wf(*, name='mcribs_dhcp_wf'):
    """
    Generate GIFTI registration files to dhcp (42-week) space.

    Note: The dhcp template was derived from the Conte69 atlas,
    and maps reasonably well to fsLR.
    """
    from smriprep.interfaces.workbench import SurfaceSphereProjectUnproject

    workflow = LiterateWorkflow(name=name)

    inputnode = pe.Node(
        niu.IdentityInterface(['sphere_reg', 'sulc']),
        name='inputnode',
    )
    outputnode = pe.Node(
        niu.IdentityInterface(['sphere_reg_fsLR']),
        name='outputnode',
    )

    # SurfaceSphereProjectUnProject
    # project to 41k dHCP atlas sphere
    #   - sphere-in: Individual native sphere in surf directory registered to 41k atlas sphere
    #   - sphere-to: the 41k atlas sphere, in the fsaverage directory
    #   - sphere-unproject-from: 41k atlas sphere registered to dHCP 42wk sphere,
    #                            in the fsaverage directory
    #   - sphere-out: lh.sphere.reg2.dHCP42.native.surf.gii
    project_unproject = pe.MapNode(
        SurfaceSphereProjectUnproject(),
        iterfield=['sphere_in', 'sphere_project_to', 'sphere_unproject_from'],
        name='project_unproject',
    )
    project_unproject.inputs.sphere_project_to = [
        str(
            tf.get(
                'fsaverage',
                density='41k',
                hemi=hemi,
                desc=None,
                suffix='sphere',
                extension='.surf.gii',
            )
        )
        for hemi in 'LR'
    ]

    project_unproject.inputs.sphere_unproject_from = [  # TODO: Use symmetric template
        str(
            tf.get(
                'dhcpAsym',
                space='fsaverage',
                hemi=hemi,
                density='41k',
                desc='reg',
                suffix='sphere',
                extension='.surf.gii',
                raise_empty=True,
            )
        )
        for hemi in 'LR'
    ]

    workflow.connect([
        (inputnode, project_unproject, [('sphere_reg', 'sphere_in')]),
        (project_unproject, outputnode, [('sphere_out', 'sphere_reg_fsLR')]),
    ])  # fmt:skip

    return workflow


def init_infantfs_surface_recon_wf(
    *,
    age_months: int,
    precomputed: dict,
    omp_nthreads: int,
    use_aseg: bool = False,
    name: str = 'infantfs_surface_recon_wf',
):
    from nibabies.interfaces.freesurfer import InfantReconAll

    workflow = LiterateWorkflow(name=name)
    inputnode = pe.Node(niu.IdentityInterface(fields=SURFACE_INPUTS), name='inputnode')
    outputnode = pe.Node(niu.IdentityInterface(fields=SURFACE_OUTPUTS), name='outputnode')

    desc = (
        'Brain surfaces were reconstructed using `infant_recon_all` [FreeSurfer '
        f'{fs.Info().looseversion() or "<ver>"}, RRID:SCR_001847, @infantfs], '
        'using the reference T1w'
    )
    desc += '.' if not use_aseg else ' and a pre-computed anatomical segmentation.'
    workflow.__desc__ = desc

    gen_recon_outdir = pe.Node(niu.Function(function=_gen_recon_dir), name='gen_recon_outdir')

    # inject the intensity-normalized skull-stripped t1w from the brain extraction workflow
    recon = pe.Node(InfantReconAll(age=age_months), name='reconall')
    if use_aseg:
        workflow.connect(inputnode, 'in_aseg', recon, 'aseg_file')

    fssource = pe.Node(FreeSurferSource(), name='fssource', run_without_submitting=True)
    midthickness_wf = init_midthickness_wf(omp_nthreads=omp_nthreads)

    workflow.connect([
        (inputnode, gen_recon_outdir, [
            ('subjects_dir', 'subjects_dir'),
            ('subject_id', 'subject_id'),
        ]),
        (inputnode, recon, [
            ('skullstripped_t1', 'mask_file'),
            ('subject_id', 'subject_id'),
        ]),
        (gen_recon_outdir, recon, [
            ('out', 'outdir'),
        ]),
        (recon, fssource, [
            ('subject_id', 'subject_id'),
            (('outdir', _parent), 'subjects_dir'),
        ]),
        (fssource, midthickness_wf, [
            ('white', 'inputnode.white'),
            ('graymid', 'inputnode.graymid'),
        ]),
        (midthickness_wf, outputnode, [
            ('outputnode.subjects_dir', 'subjects_dir'),
            ('outputnode.subject_id', 'subject_id'),
        ])
    ])  # fmt:skip

    if 'fsnative' not in precomputed.get('transforms', {}):
        fsnative2anat_xfm = pe.Node(
            RobustRegister(auto_sens=True, est_int_scale=True),
            name='fsnative2anat_xfm',
        )
        anat2fsnative_xfm = pe.Node(
            LTAConvert(out_lta=True, invert=True),
            name='anat2fsnative_xfm',
        )
        workflow.connect([
            (inputnode, fsnative2anat_xfm, [('skullstripped_t1', 'target_file')]),
            (fssource, fsnative2anat_xfm, [
                (('norm', _replace_mgz), 'source_file'),
            ]),
            (fsnative2anat_xfm, anat2fsnative_xfm, [('out_reg_file', 'in_lta')]),
            (fsnative2anat_xfm, outputnode, [
                ('out_reg_file', 'fsnative2anat_xfm'),
            ]),
            (anat2fsnative_xfm, outputnode, [
                ('out_lta', 'anat2fsnative_xfm'),
            ]),
        ])  # fmt:skip

    return workflow


def init_midthickness_wf(*, omp_nthreads: int, name: str = 'make_midthickness_wf') -> pe.Workflow:
    """
    Standalone workflow to create and save cortical midthickness, derived from
    the generated white / graymid surfaces.
    """

    workflow = pe.Workflow(name=name)
    inputnode = niu.IdentityInterface(fields=['white', 'graymid'], name='inputnode')
    outputnode = niu.IdentityInterface(fields=['subject_id', 'subjects_dir'], name='outputnode')

    midthickness = pe.MapNode(
        MakeMidthickness(thickness=True, distance=0.5, out_name='midthickness'),
        iterfield='in_file',
        name='midthickness',
        n_procs=min(omp_nthreads, 12),
    )
    save_midthickness = pe.Node(nio.DataSink(parameterization=False), name='save_midthickness')

    sync = pe.Node(
        niu.Function(
            function=_extract_fs_fields,
            output_names=['subjects_dir', 'subject_id'],
        ),
        name='sync',
    )

    workflow.connect([
        (inputnode, midthickness, [
            ('white', 'in_file'),
            ('graymid', 'graymid'),
        ]),
        (midthickness, save_midthickness, [('out_file', 'surf.@graymid')]),
        (save_midthickness, sync, [('out_file', 'filenames')]),
        (sync, outputnode, [
            ('subjects_dir', 'subjects_dir'),
            ('subject_id', 'subject_id'),
        ]),
    ])  # fmt:skip
    return workflow


def init_resample_midthickness_dhcp_wf(
    grayord_density: ty.Literal['91k', '170k'],
    name: str = 'resample_midthickness_wf',
):
    """
    Resample subject midthickness surface to specified density.

    Workflow Graph
        .. workflow::
            :graph2use: colored
            :simple_form: yes

            from nibabies.workflows.anatomical.surfaces import init_resample_midthickness_wf
            wf = init_resample_midthickness_wf(grayord_density="91k")

    Parameters
    ----------
    grayord_density : :obj:`str`
        Either `91k` or `170k`, representing the total of vertices or *grayordinates*.
    name : :obj:`str`
        Unique name for the subworkflow (default: ``"resample_midthickness_wf"``)

    Inputs
    ------
    midthickness
        GIFTI surface mesh corresponding to the midthickness surface
    sphere_reg_fsLR
        GIFTI surface mesh corresponding to the subject's fsLR registration sphere

    Outputs
    -------
    midthickness
        GIFTI surface mesh corresponding to the midthickness surface, resampled to fsLR
    """
    workflow = LiterateWorkflow(name=name)

    fslr_density = '32k' if grayord_density == '91k' else '59k'

    inputnode = pe.Node(
        niu.IdentityInterface(fields=['midthickness', 'sphere_reg_fsLR']),
        name='inputnode',
    )

    outputnode = pe.Node(niu.IdentityInterface(fields=['midthickness_fsLR']), name='outputnode')

    resampler = pe.MapNode(
        SurfaceResample(method='BARYCENTRIC'),
        iterfield=['surface_in', 'current_sphere', 'new_sphere'],
        name='resampler',
    )
    resampler.inputs.new_sphere = [
        str(
            tf.get(
                template='dhcpAsym',
                cohort='42',
                density=fslr_density,
                suffix='sphere',
                hemi=hemi,
                space=None,
                extension='.surf.gii',
            )
        )
        for hemi in ['L', 'R']
    ]

    workflow.connect([
        (inputnode, resampler, [
            ('midthickness', 'surface_in'),
            ('sphere_reg_fsLR', 'current_sphere'),
        ]),
        (resampler, outputnode, [('surface_out', 'midthickness_fsLR')]),
    ])  # fmt:skip

    return workflow


def _parent(p):
    from pathlib import Path

    return str(Path(p).parent)


def _gen_recon_dir(subjects_dir, subject_id):
    from pathlib import Path

    p = Path(subjects_dir) / subject_id
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _replace_mgz(in_file):
    return in_file.replace('.mgz', '.nii.gz')
